"""Frozen source-backed retrieval evaluation, separate from agent-facing tools.

No answer generation or integration-claim metrics are inferred from retrieval.
Embedding runs require an explicitly confirmed disposable database; lexical runs
operate entirely on exported artifacts.
"""

import hashlib
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .embeddings import HTTPEmbeddingProvider
from .extract import ALIASES, MAX_CHUNK_BYTES, _bounded_documents
from .models import Chunk, Extraction, Repository, Resource, Snapshot, SourceFile
from .store import IndexStore

MAX_ARTIFACT_BYTES = 128 * 1024 * 1024


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str, allow_nan=False
        ).encode()
    ).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrozenScan(StrictModel):
    id: str
    repo_id: str
    commit: str
    extraction_hash: str
    fetched_at: datetime


class FrozenRepository(StrictModel):
    id: str
    name: str
    url: str
    branch: str
    stars: int = Field(ge=0)
    preference: float = Field(allow_inf_nan=False)
    snapshot: FrozenScan
    files: list[SourceFile]
    resources: list[Resource]
    chunks: list[Chunk]


class DuplicateGroup(StrictModel):
    content_hash: str
    chunk_ids: list[str]


class FrozenCorpus(StrictModel):
    schema_version: Literal[1] = 1
    corpus_id: str
    created_at: datetime
    normalization: Literal["splitlines+LF"] = "splitlines+LF"
    repositories: list[FrozenRepository]
    duplicate_groups: list[DuplicateGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.repositories:
            raise ValueError("Frozen corpus requires at least one repository")
        seen_repos, seen_chunks, seen_resources, seen_files = set(), set(), set(), set()
        hashes = {}
        for repo in self.repositories:
            if repo.id in seen_repos or repo.snapshot.repo_id != repo.id:
                raise ValueError("Duplicate repository or inconsistent snapshot")
            seen_repos.add(repo.id)
            files = {file.id: file for file in repo.files}
            resources = {resource.id: resource for resource in repo.resources}
            for group, seen in (
                (repo.files, seen_files),
                (repo.resources, seen_resources),
                (repo.chunks, seen_chunks),
            ):
                for item in group:
                    if item.id in seen or item.repo_id != repo.id or item.snapshot_id != repo.snapshot.id:
                        raise ValueError("Duplicate or inconsistent source identity")
                    seen.add(item.id)
            for resource in repo.resources:
                source = files.get(resource.file_id)
                if not source or source.path != resource.path:
                    raise ValueError("Resource source is missing or inconsistent")
            for chunk in repo.chunks:
                source = files.get(chunk.file_id)
                if not source or source.path != chunk.path or not 1 <= chunk.start_line <= chunk.end_line:
                    raise ValueError("Chunk source is missing or inconsistent")
                if chunk.resource_id and chunk.resource_id not in resources:
                    raise ValueError("Chunk references an unknown resource")
                if hashlib.sha256(chunk.content.encode()).hexdigest() != chunk.content_hash:
                    raise ValueError("Chunk content hash mismatch")
                if len(chunk.content.encode()) > 32000:
                    raise ValueError("Chunk exceeds supported byte budget")
                hashes.setdefault(chunk.content_hash, []).append(chunk.id)
        expected_groups = {digest: sorted(ids) for digest, ids in hashes.items() if len(ids) > 1}
        groups = {group.content_hash: sorted(group.chunk_ids) for group in self.duplicate_groups}
        if len(groups) != len(self.duplicate_groups) or groups != expected_groups:
            raise ValueError("Duplicate grouping does not match corpus content hashes")
        payload = self.model_dump(mode="json", exclude={"created_at", "corpus_id"})
        if canonical_hash(payload) != self.corpus_id:
            raise ValueError("Frozen corpus fingerprint mismatch")
        return self


class SourceJudgment(StrictModel):
    repo_id: str
    commit: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str


class Relevance(StrictModel):
    chunk_id: str
    grade: int = Field(ge=0, le=3, strict=True)
    source: SourceJudgment


class QueryFilters(StrictModel):
    repo_ids: list[str] | None = None
    app: str | None = None
    service: str | None = None
    kind: str | None = None
    namespace: str | None = None
    context: str | None = None


class EvaluationQuery(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=8000)
    category: Literal["conceptual", "configuration", "identifier", "negative"] | None = None
    group_id: str | None = None
    filters: QueryFilters = Field(default_factory=QueryFilters)
    relevance: list[Relevance] = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def bounded_query(self):
        if len(self.text.encode()) > 8192:
            raise ValueError("Evaluation query exceeds its UTF-8 byte budget")
        return self


class Judgments(StrictModel):
    schema_version: Literal[1] = 1
    corpus_id: str
    purpose: Literal["calibration", "holdout"]
    judgment_coverage: Literal["sparse", "exhaustive"] = "sparse"
    queries: list[EvaluationQuery] = Field(min_length=1, max_length=10000)


class ProviderConfig(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    protocol: Literal["openai", "voyage", "tei"] = "openai"
    url: str
    model: str
    model_revision: str | None = None
    dimensions: int = Field(ge=1, le=16000)
    request_timeout_seconds: float = Field(default=60, ge=1, le=1800, allow_inf_nan=False)
    requested_dimensions: int | None = Field(default=None, ge=1, le=16000)
    query_instruction: str | None = None
    document_prefix: str = ""
    query_prefix: str = ""
    query_prompt_name: str | None = None
    document_prompt_name: str | None = None
    api_key_env: str | None = None

    @model_validator(mode="after")
    def validate_provider(self):
        url = urlsplit(self.url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Provider URL must be HTTP(S) without credentials, query, or fragment")
        if self.api_key_env and not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must name an environment variable")
        if self.requested_dimensions is not None and self.requested_dimensions != self.dimensions:
            raise ValueError("Requested and expected provider dimensions must agree")
        # Syntax/preprocessing validation is local and does not resolve credentials or call HTTP.
        HTTPEmbeddingProvider(**self.model_dump(exclude={"id", "api_key_env"}), api_key=None)
        return self

    def provider(self):
        key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if self.api_key_env and not key:
            raise ValueError("Selected provider credential environment variable is unset")
        arguments = self.model_dump(exclude={"id", "api_key_env"})
        return HTTPEmbeddingProvider(**arguments, api_key=key)


class Providers(StrictModel):
    schema_version: Literal[1] = 1
    providers: list[ProviderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({provider.id for provider in self.providers}) != len(self.providers):
            raise ValueError("Duplicate evaluation provider IDs")
        return self


def _read_artifact(path, model, *, yaml_file=False):
    path = Path(path)
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("Evaluation artifact exceeds its byte budget")
    with path.open("rb") as stream:
        raw = stream.read(MAX_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("Evaluation artifact exceeds its byte budget")
    if yaml_file:
        documents = _bounded_documents(raw)
        if len(documents) != 1:
            raise ValueError("Provider configuration requires one YAML document")
        data = documents[0][1]
    else:
        data = json.loads(raw)
    return model.model_validate(data), hashlib.sha256(raw).hexdigest()


def eligible_chunk_ids(corpus, query):
    filters = query.filters
    scoped = any(
        getattr(filters, field) is not None for field in ("app", "service", "kind", "namespace", "context")
    )
    eligible = set()
    for repo in corpus.repositories:
        if filters.repo_ids is not None and repo.id not in filters.repo_ids:
            continue
        resources = {resource.id: resource for resource in repo.resources}
        for chunk in repo.chunks:
            if scoped:
                resource = resources.get(chunk.resource_id)
                if not resource:
                    continue
                if any(
                    getattr(filters, field) is not None
                    and getattr(resource, field) != getattr(filters, field)
                    for field in ("app", "kind", "namespace", "context")
                ):
                    continue
                if (
                    filters.service is not None
                    and ALIASES.get(filters.service, filters.service) not in resource.services
                ):
                    continue
            eligible.add(chunk.id)
    return eligible


def structured_ranking(corpus, query, limit=100):
    if not any(
        getattr(query.filters, field) is not None
        for field in ("app", "service", "kind", "namespace", "context")
    ):
        return None
    eligible = eligible_chunk_ids(corpus, query)
    chunks = [
        (repo.stars, chunk)
        for repo in corpus.repositories
        for chunk in repo.chunks
        if chunk.id in eligible and chunk.resource_id
    ]
    return [
        {"chunk_id": chunk.id, "score": stars}
        for stars, chunk in sorted(chunks, key=lambda row: (-row[0], row[1].resource_id, row[1].id))[:limit]
    ]


def validate_judgments(corpus: FrozenCorpus, judgments: Judgments):
    if judgments.corpus_id != corpus.corpus_id:
        raise ValueError("Judgments refer to a different frozen corpus")
    chunks = {chunk.id: (repo, chunk) for repo in corpus.repositories for chunk in repo.chunks}
    repo_ids = {repo.id for repo in corpus.repositories}
    query_ids = set()
    for query in judgments.queries:
        if query.id in query_ids or not query.text.strip():
            raise ValueError("Query IDs must be unique and query text nonempty")
        query_ids.add(query.id)
        allowed = repo_ids if query.filters.repo_ids is None else set(query.filters.repo_ids)
        if not allowed.issubset(repo_ids):
            raise ValueError("Query filter names an unknown repository")
        eligible = eligible_chunk_ids(corpus, query)
        judged_ids = set()
        for relevance in query.relevance:
            if relevance.chunk_id in judged_ids or relevance.chunk_id not in chunks:
                raise ValueError("Duplicate judgment or unknown chunk")
            judged_ids.add(relevance.chunk_id)
            repo, chunk = chunks[relevance.chunk_id]
            source = {
                "repo_id": repo.id,
                "commit": repo.snapshot.commit,
                "path": chunk.path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "content_hash": chunk.content_hash,
            }
            if relevance.source.model_dump() != source or repo.id not in allowed or chunk.id not in eligible:
                raise ValueError("Judgment source provenance or query filter mismatch")
    return {
        "queries": len(judgments.queries),
        "answerable_queries": sum(any(r.grade > 0 for r in q.relevance) for q in judgments.queries),
        "purpose": judgments.purpose,
        "judgment_coverage": judgments.judgment_coverage,
    }


def validate_files(corpus_path, judgments_path, providers_path=None):
    corpus, _ = _read_artifact(corpus_path, FrozenCorpus)
    judgments, _ = _read_artifact(judgments_path, Judgments)
    result = {"corpus_id": corpus.corpus_id, **validate_judgments(corpus, judgments)}
    if providers_path:
        providers, _ = _read_artifact(providers_path, Providers, yaml_file=True)
        result["provider_ids"] = [provider.id for provider in providers.providers]
    return result


def rebind_judgments(corpus_path, judgments_path, output):
    """Move calibration judgments only when every inspected source range is unchanged."""
    corpus, _ = _read_artifact(corpus_path, FrozenCorpus)
    judgments, _ = _read_artifact(judgments_path, Judgments)
    if judgments.purpose != "calibration":
        raise ValueError("Automatic rebinding is restricted to calibration judgments")
    matches = {}
    for repo in corpus.repositories:
        for chunk in repo.chunks:
            identity = (repo.id, chunk.path, chunk.start_line, chunk.end_line, chunk.content_hash)
            matches.setdefault(identity, []).append((repo, chunk))
    updated = judgments.model_dump(mode="json")
    updated["corpus_id"] = corpus.corpus_id
    for query in updated["queries"]:
        for relevance in query["relevance"]:
            source = relevance["source"]
            identity = tuple(
                source[key] for key in ("repo_id", "path", "start_line", "end_line", "content_hash")
            )
            candidates = matches.get(identity, [])
            if len(candidates) != 1:
                raise ValueError("Calibration evidence is missing or ambiguous in the new corpus")
            repo, chunk = candidates[0]
            relevance["chunk_id"] = chunk.id
            source["commit"] = repo.snapshot.commit
    rebound = Judgments.model_validate(updated)
    validation = validate_judgments(corpus, rebound)
    _write_json(output, rebound.model_dump(mode="json"), overwrite=False)
    return {
        "previous_corpus_id": judgments.corpus_id,
        "corpus_id": corpus.corpus_id,
        "output": str(output),
        **validation,
    }


def _live_signatures(store, repo_ids):
    rows = store._query(
        "SELECT p.id,s.id AS snapshot_id,s.commit_sha,s.extraction_hash FROM repositories p "
        "JOIN snapshots s ON s.id=p.current_snapshot WHERE p.id=ANY(%s) ORDER BY p.id",
        (repo_ids,),
    )
    return [(row["id"], row["snapshot_id"], row["commit_sha"], row["extraction_hash"]) for row in rows]


def assert_live_corpus(corpus, store):
    expected = sorted(
        (repo.id, repo.snapshot.id, repo.snapshot.commit, repo.snapshot.extraction_hash)
        for repo in corpus.repositories
    )
    if _live_signatures(store, [repo.id for repo in corpus.repositories]) != expected:
        raise ValueError("Live corpus changed; export a new frozen corpus before evaluating latest state")


def export_corpus(store, corpus_store, output: Path, repo_ids=None, max_bytes=64 * 1024 * 1024):
    if not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
        raise ValueError("Export byte budget must be 1..128MiB")
    repositories = []
    used = 0
    with store._connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        query = (
            "SELECT p.*,s.commit_sha,s.extraction_hash,s.fetched_at FROM repositories p "
            "JOIN snapshots s ON s.id=p.current_snapshot"
        )
        rows = conn.execute(
            query + (" WHERE p.id=ANY(%s)" if repo_ids is not None else "") + " ORDER BY p.id LIMIT 1001",
            (repo_ids,) if repo_ids is not None else (),
        ).fetchall()
        if len(rows) > 1000:
            raise ValueError("Select at most 1000 repositories for a frozen export")
        if repo_ids is not None and set(repo_ids) != {row["id"] for row in rows}:
            raise ValueError("Selected repository has no current indexed scan")
        for row in rows:
            snapshot = FrozenScan(
                id=row["current_snapshot"],
                repo_id=row["id"],
                commit=row["commit_sha"],
                extraction_hash=row["extraction_hash"],
                fetched_at=row["fetched_at"],
            )
            items = {}
            for table in ("files", "resources", "chunks"):
                payload = "payload-'document'" if table == "resources" else "payload"
                with conn.cursor(name=f"export_{table}") as cursor:
                    cursor.itersize = 100
                    cursor.execute(
                        f"SELECT {payload} AS payload FROM {table} WHERE snapshot_id=%s ORDER BY id",
                        (snapshot.id,),
                    )
                    items[table] = []
                    for result in cursor:
                        used += len(json.dumps(result["payload"], default=str).encode())
                        if used > max_bytes:
                            raise ValueError(
                                "Frozen corpus export exceeds its byte budget; select fewer repos"
                            )
                        items[table].append(result["payload"])
            repositories.append(
                FrozenRepository(
                    id=row["id"],
                    name=row["name"],
                    url=row["url"],
                    branch=row["branch"],
                    stars=row["stars"],
                    preference=row["preference"],
                    snapshot=snapshot,
                    **items,
                )
            )
    hashes = {}
    for repo in repositories:
        snapshot = Snapshot.model_validate(repo.snapshot.model_dump())
        required = {chunk.file_id for chunk in repo.chunks}
        batch = corpus_store.read_many(snapshot, [file for file in repo.files if file.id in required])
        sources = {file.id: text for file, text in batch["texts"]}
        for chunk in repo.chunks:
            raw = sources.get(chunk.file_id)
            if (
                raw is None
                or "\n".join(raw.splitlines()[chunk.start_line - 1 : chunk.end_line]) != chunk.content
            ):
                raise ValueError("Indexed chunk does not match commit-pinned retained source")
            hashes.setdefault(chunk.content_hash, []).append(chunk.id)
    payload = {
        "schema_version": 1,
        "normalization": "splitlines+LF",
        "repositories": [repo.model_dump(mode="json") for repo in repositories],
        "duplicate_groups": [
            {"content_hash": digest, "chunk_ids": sorted(ids)}
            for digest, ids in sorted(hashes.items())
            if len(ids) > 1
        ],
    }
    corpus = FrozenCorpus.model_validate(
        {**payload, "corpus_id": canonical_hash(payload), "created_at": datetime.now(UTC)}
    )
    assert_live_corpus(corpus, store)
    encoded = _encoded_json(corpus.model_dump(mode="json")).encode()
    if len(encoded) > max_bytes:
        raise ValueError("Serialized export exceeds byte budget")
    _write_json(output, corpus.model_dump(mode="json"))
    return {
        "corpus_id": corpus.corpus_id,
        "repositories": len(repositories),
        "chunks": sum(len(repo.chunks) for repo in repositories),
        "bytes": len(encoded),
        "output": str(output),
    }


def _encoded_json(value):
    return json.dumps(value, indent=2, allow_nan=False, default=str) + "\n"


def _write_json(path, value, *, overwrite=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(_encoded_json(value))
        if overwrite:
            temporary.replace(path)
        else:
            # Atomic refusal if another writer or an existing artifact owns this path.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _tokens(text):
    return re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", text.lower())


class LexicalIndex:
    """BM25 baseline over raw chunk text plus its source path."""

    def __init__(self, corpus):
        self.corpus = corpus
        self.chunks = {chunk.id: chunk for repo in corpus.repositories for chunk in repo.chunks}
        self.counts = {
            id: Counter(_tokens(chunk.path + "\n" + chunk.content)) for id, chunk in self.chunks.items()
        }
        self.lengths = {id: sum(counts.values()) for id, counts in self.counts.items()}
        self.average_length = sum(self.lengths.values()) / max(1, len(self.lengths))
        self.frequencies = Counter(term for counts in self.counts.values() for term in counts)

    def search(self, query, limit=100):
        scores = []
        allowed = eligible_chunk_ids(self.corpus, query)
        for id, counts in self.counts.items():
            if id not in allowed:
                continue
            score = 0
            for token in set(_tokens(query.text)):
                frequency = counts.get(token, 0)
                if frequency:
                    inverse = math.log(
                        1
                        + (len(self.chunks) - self.frequencies[token] + 0.5) / (self.frequencies[token] + 0.5)
                    )
                    denominator = frequency + 1.2 * (
                        1 - 0.75 + 0.75 * self.lengths[id] / max(1, self.average_length)
                    )
                    score += inverse * frequency * 2.2 / denominator
            if score:
                scores.append({"chunk_id": id, "score": score})
        return sorted(scores, key=lambda row: (-row["score"], row["chunk_id"]))[:limit]


def reciprocal_rank_fusion(*rankings, limit=100, constant=60):
    scores = Counter()
    for ranking in rankings:
        for rank, row in enumerate(ranking, 1):
            scores[row["chunk_id"]] += 1 / (constant + rank)
    return [
        {"chunk_id": id, "score": score}
        for id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def metrics(ranking, query, k=10):
    grades = {item.chunk_id: item.grade for item in query.relevance}
    positives = {id for id, grade in grades.items() if grade > 0}
    ids = list(dict.fromkeys(row["chunk_id"] for row in ranking))[:k]
    exposure = {
        "retrieved_results": len(ids),
        "judged_negative_results": sum(grades.get(id) == 0 for id in ids),
        "unjudged_results": sum(id not in grades for id in ids),
    }
    if not positives:
        return {"answerable": False, "recall_at_10": None, "mrr_at_10": None, "ndcg_at_10": None, **exposure}
    ideal = sorted((2**grade - 1 for grade in grades.values() if grade > 0), reverse=True)[:k]
    dcg = sum((2 ** grades.get(id, 0) - 1) / math.log2(rank + 1) for rank, id in enumerate(ids, 1))
    idcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    return {
        "answerable": True,
        "recall_at_10": len(positives.intersection(ids)) / len(positives),
        "mrr_at_10": next((1 / rank for rank, id in enumerate(ids, 1) if id in positives), 0),
        "ndcg_at_10": dcg / idcg,
        **exposure,
    }


def _summarize(results):
    answerable = [row for row in results if row["metrics"]["answerable"]]
    aggregate = {
        field: sum(row["metrics"][field] for row in answerable) / len(answerable) if answerable else None
        for field in ("recall_at_10", "mrr_at_10", "ndcg_at_10")
    }
    latencies = sorted(row["latency_seconds"] for row in results)
    return {
        **aggregate,
        "answerable_queries": len(answerable),
        "no_positive_queries": len(results) - len(answerable),
        "retrieval_exposure": {
            field: sum(row["metrics"][field] for row in results)
            for field in ("retrieved_results", "judged_negative_results", "unjudged_results")
        },
        "latency_seconds": {
            "mean": sum(latencies) / len(latencies) if latencies else None,
            "p95": latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)] if latencies else None,
        },
        "queries": results,
    }


@contextmanager
def disposable_index(database_url, confirmed=False):
    if not confirmed or not database_url:
        raise ValueError(
            "Dense evaluation requires EXPLORER_EVAL_DATABASE_URL and explicit disposable confirmation"
        )
    schema = "eval_" + uuid.uuid4().hex
    with psycopg.connect(database_url, connect_timeout=10) as connection:
        # The extension must already be installed by the dedicated test database's owner.
        if not connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
            raise ValueError("Disposable evaluation database must have pgvector installed")
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    store = IndexStore(database_url)
    connect = store._connect

    def connection():
        result = connect()
        result.execute(sql.SQL("SET search_path TO {},public").format(sql.Identifier(schema)))
        return result

    store._connect = connection
    try:
        store.initialize()
        yield store
    finally:
        with psycopg.connect(database_url, connect_timeout=10) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def run_evaluation(
    corpus_path,
    judgments_path,
    output,
    mode="lexical",
    providers_path=None,
    provider_ids=None,
    database_url=None,
    confirmed=False,
    live_store=None,
):
    if mode not in {"lexical", "dense", "hybrid"}:
        raise ValueError("Evaluation mode must be lexical, dense, or hybrid")
    if any(
        path is not None and Path(output).resolve() == Path(path).resolve()
        for path in (corpus_path, judgments_path, providers_path)
    ):
        raise ValueError("Evaluation output must be separate from its frozen input artifacts")
    corpus, corpus_digest = _read_artifact(corpus_path, FrozenCorpus)
    judgments, judgments_digest = _read_artifact(judgments_path, Judgments)
    validation = validate_judgments(corpus, judgments)
    if mode != "lexical" and any(
        len(chunk.content.encode()) > MAX_CHUNK_BYTES for repo in corpus.repositories for chunk in repo.chunks
    ):
        raise ValueError(
            f"Frozen corpus contains documents over {MAX_CHUNK_BYTES} UTF-8 bytes; "
            "rescan and export with the current provider-neutral chunker before dense evaluation"
        )
    if live_store:
        assert_live_corpus(corpus, live_store)
    started = time.perf_counter()
    lexical = LexicalIndex(corpus)
    unique_documents = {chunk.content_hash: chunk.content for chunk in lexical.chunks.values()}
    lexical_index_seconds = time.perf_counter() - started
    lexical_results, lexical_rankings, lexical_latencies, structured_results = [], {}, {}, []
    for query in judgments.queries:
        start = time.perf_counter()
        ranking = lexical.search(query)
        elapsed = time.perf_counter() - start
        lexical_rankings[query.id] = ranking
        lexical_latencies[query.id] = elapsed
        start = time.perf_counter()
        structured = structured_ranking(corpus, query)
        structured_elapsed = time.perf_counter() - start
        if structured is not None:
            structured_results.append(
                {
                    "query_id": query.id,
                    "category": query.category,
                    "group_id": query.group_id,
                    "filters": query.filters.model_dump(),
                    "latency_seconds": structured_elapsed,
                    "ranking": structured[:10],
                    "metrics": metrics(structured, query),
                }
            )
        lexical_results.append(
            {
                "query_id": query.id,
                "category": query.category,
                "group_id": query.group_id,
                "filters": query.filters.model_dump(),
                "latency_seconds": elapsed,
                "ranking": ranking[:10],
                "metrics": metrics(ranking, query),
            }
        )
    report = {
        "schema_version": 1,
        "corpus_id": corpus.corpus_id,
        "judgments_hash": judgments_digest,
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": judgments.purpose,
        "judgment_coverage": judgments.judgment_coverage,
        "validation": validation,
        "metric_convention": (
            "Unjudged results count as zero gain; no-positive queries excluded from relevance averages"
        ),
        "lexical": {
            "method": "BM25",
            "input_fields": ["path", "content"],
            "latency_scope": "In-memory tokenization/scoring, source filters, and ranking",
            "k1": 1.2,
            "b": 0.75,
            "indexing_seconds": lexical_index_seconds,
            **_summarize(lexical_results),
        },
        "providers": [],
        "structured": {
            "method": "Exact explicit predicates, stars then resource identity",
            "latency_scope": "Frozen in-memory resource predicates and chunk ranking",
            "not_applicable_queries": len(judgments.queries) - len(structured_results),
            **_summarize(structured_results),
        },
        "corpus_statistics": {
            "repositories": len(corpus.repositories),
            "chunks": len(lexical.chunks),
            "exact_duplicate_groups": len(corpus.duplicate_groups),
            "source_bytes": sum(file.size for repo in corpus.repositories for file in repo.files),
            "unique_document_texts": len(unique_documents),
            "unique_document_bytes": sum(len(content.encode()) for content in unique_documents.values()),
            "unique_document_lexical_tokens": sum(
                len(_tokens(content)) for content in unique_documents.values()
            ),
        },
        "quality_limits": [
            "Sparse calibration judgments are not final retrieval quality",
            "Retrieval relevance does not measure answer success or integration claims",
        ],
    }
    provider_digest = None
    if mode != "lexical":
        if not providers_path:
            raise ValueError("Dense evaluation requires a provider configuration")
        if not confirmed or not database_url:
            raise ValueError(
                "Dense evaluation requires a dedicated evaluation database and explicit confirmation"
            )
        if not provider_ids:
            raise ValueError("Select evaluation providers explicitly with --provider IDs")
        providers, provider_digest = _read_artifact(providers_path, Providers, yaml_file=True)
        selected = [p for p in providers.providers if provider_ids is None or p.id in provider_ids]
        if not selected or (provider_ids and set(provider_ids) - {p.id for p in selected}):
            raise ValueError("Requested evaluation provider does not exist")
        prepared = [(configuration, configuration.provider()) for configuration in selected]
        # All selected providers and query/document inputs pass local validation before any calls.
        preflights = {}
        documents = list(unique_documents.values())
        for configuration, provider in prepared:
            document_bytes = 0
            for offset in range(0, len(documents), 16):
                batch = provider._prepare(documents[offset : offset + 16], "document")
                document_bytes += sum(len(text.encode()) for text in batch)
            query_bytes = sum(
                len(provider._prepare([query.text], "query")[0].encode()) for query in judgments.queries
            )
            preflights[configuration.id] = {
                "unique_document_texts": len(documents),
                "prepared_document_bytes": document_bytes,
                "query_texts": len(judgments.queries),
                "prepared_query_bytes": query_bytes,
                "token_counts": "Unknown until reported by provider; lexical counts are not model tokens",
            }
        for configuration, provider in prepared:
            with disposable_index(database_url, confirmed) as index:
                for repo in corpus.repositories:
                    index.publish(
                        Repository(**repo.model_dump(exclude={"snapshot", "files", "resources", "chunks"})),
                        Snapshot.model_validate(repo.snapshot.model_dump()),
                        repo.files,
                        Extraction(resources=repo.resources, chunks=repo.chunks),
                    )
                start = time.perf_counter()
                for repo in corpus.repositories:
                    index.index_embeddings(repo.snapshot.id, provider)
                index_seconds = time.perf_counter() - start
                dense_results, fusion_results = [], []
                for query in judgments.queries:
                    start = time.perf_counter()
                    items = index.semantic_search(
                        query.text,
                        provider,
                        repo_ids=query.filters.repo_ids,
                        limit=100,
                        chunk_ids=sorted(eligible_chunk_ids(corpus, query)),
                    )
                    if not items.get("available"):
                        raise ValueError(
                            "Frozen semantic index changed or became unavailable during evaluation"
                        )
                    ranking = [
                        {"chunk_id": row["id"], "score": row["cosine_similarity"]} for row in items["items"]
                    ]
                    elapsed = time.perf_counter() - start
                    result = {
                        "query_id": query.id,
                        "category": query.category,
                        "group_id": query.group_id,
                        "filters": query.filters.model_dump(),
                        "latency_seconds": elapsed,
                        "ranking": ranking[:10],
                        "metrics": metrics(ranking, query),
                    }
                    dense_results.append(result)
                    if mode == "hybrid":
                        start = time.perf_counter()
                        fused = reciprocal_rank_fusion(lexical_rankings[query.id], ranking)
                        fusion_elapsed = time.perf_counter() - start
                        fusion_results.append(
                            {
                                **result,
                                "latency_seconds": lexical_latencies[query.id] + elapsed + fusion_elapsed,
                                "ranking": fused[:10],
                                "metrics": metrics(fused, query),
                            }
                        )
                run = {
                    "configuration_id": configuration.id,
                    "provider": provider.metadata,
                    "usage": provider.usage_stats,
                    "preflight": preflights[configuration.id],
                    "indexing_seconds": index_seconds,
                    "dense": {
                        "method": "exact_pgvector_cosine",
                        "input_fields": ["content"],
                        "latency_scope": "Query HTTP embedding plus exact PostgreSQL filters/ranking",
                        **_summarize(dense_results),
                    },
                }
                if mode == "hybrid":
                    run["hybrid"] = {
                        "method": "lexical+dense RRF",
                        "constant": 60,
                        "latency_scope": "Sequential sum of lexical, dense, and fusion query time",
                        **_summarize(fusion_results),
                    }
                report["providers"].append(run)
    for path, digest in (
        (corpus_path, corpus_digest),
        (judgments_path, judgments_digest),
        (providers_path, provider_digest),
    ):
        if digest and hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError("Evaluation artifacts changed during benchmark; discard results")
    if live_store:
        assert_live_corpus(corpus, live_store)
    _write_json(output, report)
    return report
