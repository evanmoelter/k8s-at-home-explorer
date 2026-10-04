"""Evaluation mechanics/provenance fixtures, not semantic quality benchmarks."""

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime

import psycopg
import pytest
from pydantic import ValidationError

from k8s_explorer import cli, evaluation
from k8s_explorer.corpus import CorpusStore
from k8s_explorer.extract import extract
from k8s_explorer.models import Repository, Snapshot, SourceFile, stable_id


def frozen_payload(repo, snapshot, files, extraction, extraction_hash="parsed-v1"):
    hashes = {}
    for chunk in extraction.chunks:
        hashes.setdefault(chunk.content_hash, []).append(chunk.id)
    payload = {
        "schema_version": 1,
        "normalization": "splitlines+LF",
        "repositories": [
            {
                **repo.model_dump(mode="json"),
                "snapshot": {**snapshot.model_dump(mode="json"), "extraction_hash": extraction_hash},
                "files": [file.model_dump() for file in files],
                "resources": [resource.model_dump() for resource in extraction.resources],
                "chunks": [chunk.model_dump() for chunk in extraction.chunks],
            }
        ],
        "duplicate_groups": [
            {"content_hash": digest, "chunk_ids": sorted(ids)}
            for digest, ids in hashes.items()
            if len(ids) > 1
        ],
    }
    return {
        **payload,
        "corpus_id": evaluation.canonical_hash(payload),
        "created_at": datetime.now(UTC).isoformat(),
    }


def evidence(repo, chunk, grade):
    return {
        "chunk_id": chunk.id,
        "grade": grade,
        "source": {
            "repo_id": repo.id,
            "commit": repo.snapshot.commit,
            "path": chunk.path,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "content_hash": chunk.content_hash,
        },
    }


@pytest.fixture
def artifacts(tmp_path):
    repo = Repository(id=stable_id("unit"), name="unit", url="https://github.com/example/unit", stars=10)
    snapshot = Snapshot(id=stable_id("scan"), repo_id=repo.id, commit="a" * 40)
    texts = [
        (
            "kubernetes/apollo/apps/backup.yaml",
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: volsync\n"
            "  namespace: apps\nspec:\n  containers:\n  - image: ghcr.io/example/volsync:latest",
        ),
        (
            "kubernetes/apollo/apps/database.yaml",
            "apiVersion: postgresql.cnpg.io/v1\nkind: Cluster\n"
            "metadata:\n  name: database\n  namespace: apps\nspec:\n  instances: 3",
        ),
        ("README.md", "Operator notes for backup scheduling"),
        ("docs/backup.md", "Operator notes for backup scheduling"),
    ]
    files = [
        SourceFile(
            id=stable_id(path),
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            path=path,
            blob=hashlib.sha1(text.encode()).hexdigest(),
            size=len(text.encode()),
        )
        for path, text in texts
    ]
    extracted = extract(snapshot, list(zip(files, [text for _, text in texts], strict=True)))
    corpus = evaluation.FrozenCorpus.model_validate(frozen_payload(repo, snapshot, files, extracted))
    frozen_repo = corpus.repositories[0]
    by_path = {chunk.path: chunk for chunk in frozen_repo.chunks}
    backup, database = by_path[texts[0][0]], by_path[texts[1][0]]
    judgments = evaluation.Judgments.model_validate(
        {
            "corpus_id": corpus.corpus_id,
            "purpose": "calibration",
            "queries": [
                {
                    "id": "backup",
                    "text": "volsync backup",
                    "category": "identifier",
                    "group_id": "backup",
                    "filters": {"service": "volsync"},
                    "relevance": [evidence(frozen_repo, backup, 3)],
                },
                {
                    "id": "database",
                    "text": "database instances",
                    "category": "configuration",
                    "relevance": [evidence(frozen_repo, database, 2), evidence(frozen_repo, backup, 0)],
                },
                {
                    "id": "negative",
                    "text": "definitely absent quantized fission reactor",
                    "category": "negative",
                    "relevance": [evidence(frozen_repo, database, 0)],
                },
            ],
        }
    )
    corpus_path, judgments_path = tmp_path / "corpus.json", tmp_path / "judgments.json"
    corpus_path.write_text(corpus.model_dump_json())
    judgments_path.write_text(judgments.model_dump_json())
    return corpus, judgments, corpus_path, judgments_path


def test_source_provenance_fingerprint_and_duplicate_groups(artifacts):
    corpus, judgments, _, _ = artifacts
    assert len(corpus.duplicate_groups) == 1
    assert evaluation.validate_judgments(corpus, judgments)["answerable_queries"] == 2
    changed = judgments.model_copy(deep=True)
    changed.queries[0].relevance[0].source.commit = "b" * 40
    with pytest.raises(ValueError, match="provenance"):
        evaluation.validate_judgments(corpus, changed)
    changed_corpus = corpus.model_dump(mode="json")
    changed_corpus["repositories"][0]["stars"] += 1
    with pytest.raises(ValidationError, match="fingerprint"):
        evaluation.FrozenCorpus.model_validate(changed_corpus)
    changed_corpus = corpus.model_dump(mode="json")
    changed_corpus["duplicate_groups"] = []
    with pytest.raises(ValidationError, match="Duplicate grouping"):
        evaluation.FrozenCorpus.model_validate(changed_corpus)


def test_metrics_graded_ranking_and_no_positive_exposure(artifacts):
    _, judgments, _, _ = artifacts
    query = judgments.queries[1]
    positive, negative = [item.chunk_id for item in query.relevance]
    result = evaluation.metrics(
        [{"chunk_id": negative}, {"chunk_id": "unjudged"}, {"chunk_id": positive}, {"chunk_id": positive}],
        query,
    )
    assert result["recall_at_10"] == 1
    assert result["mrr_at_10"] == pytest.approx(1 / 3)
    assert result["ndcg_at_10"] == pytest.approx(1 / 2)
    assert result["retrieved_results"] == 3
    assert result["judged_negative_results"] == result["unjudged_results"] == 1
    no_positive = evaluation.metrics([{"chunk_id": positive}], judgments.queries[2])
    assert not no_positive["answerable"]
    assert no_positive["recall_at_10"] is None
    assert no_positive["judged_negative_results"] == 1


def test_lexical_structured_uniform_filters_and_fusion(artifacts):
    corpus, judgments, _, _ = artifacts
    query = judgments.queries[0]
    positive = query.relevance[0].chunk_id
    lexical = evaluation.LexicalIndex(corpus).search(query)
    assert [row["chunk_id"] for row in lexical] == [positive]
    assert evaluation.structured_ranking(corpus, query)[0]["chunk_id"] == positive
    assert evaluation.structured_ranking(corpus, judgments.queries[1]) is None
    fused = evaluation.reciprocal_rank_fusion(
        [{"chunk_id": "a"}, {"chunk_id": "b"}], [{"chunk_id": "b"}, {"chunk_id": "c"}]
    )
    assert [row["chunk_id"] for row in fused] == ["b", "a", "c"]
    assert fused[0]["score"] == pytest.approx(1 / 61 + 1 / 62)


def test_lexical_cli_needs_no_application_config_or_database(artifacts, tmp_path, monkeypatch, capsys):
    _, _, corpus_path, judgments_path = artifacts

    def forbidden():
        raise AssertionError("Lexical evaluation must not read application configuration")

    monkeypatch.setattr(cli, "Settings", forbidden)
    monkeypatch.setattr(evaluation.psycopg, "connect", forbidden)
    monkeypatch.setenv("EXPLORER_EMBEDDING_URL", "https://incomplete.invalid/embed")
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "k8s-explorer",
            "eval",
            "run",
            "--corpus",
            str(corpus_path),
            "--judgments",
            str(judgments_path),
            "--output",
            str(output),
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["provider_runs"] == 0
    report = json.loads(output.read_text())
    assert report["lexical"]["answerable_queries"] == 2
    assert report["lexical"]["no_positive_queries"] == 1
    assert report["structured"]["not_applicable_queries"] == 2
    assert report["structured"]["queries"][0]["latency_seconds"] > 0
    assert report["lexical"]["recall_at_10"] == 1
    assert report["corpus_statistics"]["unique_document_texts"] == 3


def test_drifted_artifacts_abort_without_output(artifacts, tmp_path, monkeypatch):
    _, _, corpus_path, judgments_path = artifacts
    search = evaluation.LexicalIndex.search

    def drift(index, query, limit=100):
        with judgments_path.open("a") as stream:
            stream.write(" ")
        return search(index, query, limit)

    monkeypatch.setattr(evaluation.LexicalIndex, "search", drift)
    output = tmp_path / "report.json"
    with pytest.raises(ValueError, match="changed during"):
        evaluation.run_evaluation(corpus_path, judgments_path, output)
    assert not output.exists()


def test_report_cannot_replace_frozen_input(artifacts):
    _, _, corpus_path, judgments_path = artifacts
    before = corpus_path.read_bytes()
    with pytest.raises(ValueError, match="separate"):
        evaluation.run_evaluation(corpus_path, judgments_path, corpus_path)
    assert corpus_path.read_bytes() == before


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "https://host/embed?token=secret"},
        {"url": "https://secret:password@host/embed"},
        {"api_key": "secret"},
        {"protocol": "openai", "query_prompt_name": "query"},
        {"protocol": "tei", "query_prompt_name": "query", "query_prefix": "prefix"},
        {"model": ""},
        {"requested_dimensions": 3},
    ],
)
def test_provider_configuration_rejects_secrets_and_role_conflicts(fields):
    with pytest.raises(ValidationError):
        evaluation.ProviderConfig.model_validate(
            {
                "id": "provider",
                "url": "http://localhost:8080/embed",
                "model": "test",
                "dimensions": 2,
                **fields,
            }
        )


def test_query_byte_budget_and_confirmation_prevent_network(artifacts, tmp_path, monkeypatch):
    _, judgments, corpus_path, judgments_path = artifacts
    query = judgments.queries[0].model_dump()
    query["text"] = "界" * 3000
    with pytest.raises(ValidationError, match="byte budget"):
        evaluation.EvaluationQuery.model_validate(query)

    def forbidden(*args, **kwargs):
        raise AssertionError("No network before dedicated database confirmation")

    monkeypatch.setattr(evaluation.psycopg, "connect", forbidden)
    with pytest.raises(ValueError, match="confirmation"):
        with evaluation.disposable_index("postgresql://localhost/disposable"):
            pass
    with pytest.raises(ValueError, match="dedicated"):
        evaluation.run_evaluation(
            corpus_path,
            judgments_path,
            tmp_path / "report.json",
            mode="dense",
            providers_path="unused",
            provider_ids=["p"],
        )


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
def test_legacy_oversized_documents_require_rescan_before_provider_calls(
    artifacts, tmp_path, monkeypatch, mode
):
    corpus, judgments, corpus_path, judgments_path = artifacts
    changed = corpus.model_dump(mode="json")
    judged = judgments.queries[0].relevance[0].chunk_id
    chunk = next(chunk for chunk in changed["repositories"][0]["chunks"] if chunk["id"] == judged)
    chunk["content"] += "x" * evaluation.MAX_CHUNK_BYTES
    chunk["content_hash"] = hashlib.sha256(chunk["content"].encode()).hexdigest()
    for query in judgments.queries:
        for relevance in query.relevance:
            if relevance.chunk_id == judged:
                relevance.source.content_hash = chunk["content_hash"]
    changed["corpus_id"] = evaluation.canonical_hash(
        {k: v for k, v in changed.items() if k not in {"created_at", "corpus_id"}}
    )
    judgments.corpus_id = changed["corpus_id"]
    corpus_path.write_text(json.dumps(changed))
    judgments_path.write_text(judgments.model_dump_json())

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized frozen input must fail before provider setup, network, or database access")

    monkeypatch.setattr(evaluation.ProviderConfig, "provider", forbidden)
    monkeypatch.setattr(evaluation.HTTPEmbeddingProvider, "embed_documents", forbidden)
    monkeypatch.setattr(evaluation.HTTPEmbeddingProvider, "embed_query", forbidden)
    monkeypatch.setattr(evaluation.psycopg, "connect", forbidden)
    with pytest.raises(ValueError, match="rescan and export"):
        evaluation.run_evaluation(
            corpus_path,
            judgments_path,
            tmp_path / "dense.json",
            mode=mode,
            providers_path=tmp_path / "unused-provider.yaml",
            provider_ids=["selected"],
            database_url="postgresql://example.invalid/disposable",
            confirmed=True,
        )
    # Legacy frozen artifacts remain usable for local validation, lexical runs, and calibration rebinding.
    assert evaluation.validate_files(corpus_path, judgments_path)["corpus_id"] == judgments.corpus_id
    evaluation.run_evaluation(corpus_path, judgments_path, tmp_path / "lexical.json")
    evaluation.rebind_judgments(corpus_path, judgments_path, tmp_path / "rebound.json")


def test_rebind_calibration_requires_identical_unambiguous_evidence(artifacts, tmp_path):
    corpus, judgments, corpus_path, judgments_path = artifacts
    changed = corpus.model_dump(mode="json")
    repo = changed["repositories"][0]
    repo["snapshot"]["commit"] = "b" * 40
    repo["snapshot"]["id"] = "new-scan"
    for table in ("files", "resources", "chunks"):
        for row in repo[table]:
            row["snapshot_id"] = "new-scan"
    repo["chunks"][0]["id"] = "new-first-chunk"
    for group in changed["duplicate_groups"]:
        group["chunk_ids"] = [
            "new-first-chunk" if id == corpus.repositories[0].chunks[0].id else id
            for id in group["chunk_ids"]
        ]
    changed["corpus_id"] = evaluation.canonical_hash(
        {k: v for k, v in changed.items() if k not in {"created_at", "corpus_id"}}
    )
    corpus_path.write_text(json.dumps(changed))
    output = tmp_path / "rebound.json"
    result = evaluation.rebind_judgments(corpus_path, judgments_path, output)
    assert result["corpus_id"] != result["previous_corpus_id"]
    rebound = evaluation.Judgments.model_validate_json(output.read_text())
    assert rebound.queries[0].relevance[0].source.commit == "b" * 40
    with pytest.raises(FileExistsError):
        evaluation.rebind_judgments(corpus_path, judgments_path, output)
    judgments.purpose = "holdout"
    judgments_path.write_text(judgments.model_dump_json())
    with pytest.raises(ValueError, match="calibration"):
        evaluation.rebind_judgments(corpus_path, judgments_path, tmp_path / "holdout.json")


@pytest.mark.parametrize("mode", ["missing", "ambiguous"])
def test_rebind_refuses_missing_or_ambiguous_ranges(artifacts, tmp_path, mode):
    corpus, judgments, corpus_path, judgments_path = artifacts
    changed = corpus.model_dump(mode="json")
    repo = changed["repositories"][0]
    judged = judgments.queries[0].relevance[0].chunk_id
    chunk = next(chunk for chunk in repo["chunks"] if chunk["id"] == judged)
    if mode == "missing":
        chunk["content"] += "\n# changed inspected source"
        chunk["content_hash"] = hashlib.sha256(chunk["content"].encode()).hexdigest()
    else:
        repo["chunks"].append({**chunk, "id": "ambiguous-copy"})
        changed["duplicate_groups"].append(
            {"content_hash": chunk["content_hash"], "chunk_ids": [chunk["id"], "ambiguous-copy"]}
        )
    changed["corpus_id"] = evaluation.canonical_hash(
        {k: v for k, v in changed.items() if k not in {"created_at", "corpus_id"}}
    )
    corpus_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="missing or ambiguous"):
        evaluation.rebind_judgments(corpus_path, judgments_path, tmp_path / "rebound.json")


def test_validate_provider_syntax_does_not_resolve_credentials(artifacts, tmp_path, monkeypatch):
    _, _, corpus_path, judgments_path = artifacts
    provider_path = tmp_path / "providers.yaml"
    provider_path.write_text("""schema_version: 1
providers:
  - id: optional
    url: https://embedding.example.invalid/embed
    model: model
    dimensions: 2
    api_key_env: UNSET_EVAL_API_KEY
""")
    monkeypatch.delenv("UNSET_EVAL_API_KEY", raising=False)
    assert evaluation.validate_files(corpus_path, judgments_path, provider_path)["provider_ids"] == [
        "optional"
    ]
    with pytest.raises(ValueError, match="credential"):
        evaluation.run_evaluation(
            corpus_path,
            judgments_path,
            tmp_path / "report.json",
            mode="dense",
            providers_path=provider_path,
            provider_ids=["optional"],
            confirmed=True,
            database_url="postgresql://example.invalid/disposable",
        )


@pytest.mark.integration
def test_export_actual_git_sources_and_refuse_changed_latest_scan(tmp_path):
    url = os.environ.get("EXPLORER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires a disposable PostgreSQL+pgvector database")
    upstream = tmp_path / "upstream"
    upstream.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(upstream), *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Evaluation fixture")
    git("config", "user.email", "evaluation@example.invalid")
    source = upstream / "app.yaml"
    source.write_text("apiVersion: v1\nkind: Pod\nmetadata: {name: volsync}\n")
    git("add", ".")
    git("commit", "-m", "initial")
    repo = Repository(id=stable_id("evaluation"), name="evaluation", url=str(upstream))
    corpus_store = CorpusStore(tmp_path / "retained", ("github.com",), allow_local=True)
    # The disposable test database owner provisions extensions; benchmark runs require them beforehand.
    with psycopg.connect(url) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with evaluation.disposable_index(url, confirmed=True) as store:
        snapshot = corpus_store.sync(repo)
        files = corpus_store.files(snapshot)
        extracted = extract(snapshot, corpus_store.read_many(snapshot, files)["texts"])
        store.publish(repo, snapshot, files, extracted)
        output = tmp_path / "export.json"
        result = evaluation.export_corpus(store, corpus_store, output)
        corpus, _ = evaluation._read_artifact(output, evaluation.FrozenCorpus)
        assert result["chunks"] == 1
        assert corpus.repositories[0].snapshot.commit == git("rev-parse", "HEAD")
        assert corpus.repositories[0].chunks[0].content == source.read_text().rstrip("\n")
        evaluation.assert_live_corpus(corpus, store)
        source.write_text(source.read_text() + "spec: {hostNetwork: true}\n")
        git("add", ".")
        git("commit", "-m", "advance")
        snapshot = corpus_store.sync(repo)
        files = corpus_store.files(snapshot)
        extracted = extract(snapshot, corpus_store.read_many(snapshot, files)["texts"])
        store.publish(repo, snapshot, files, extracted)
        with pytest.raises(ValueError, match="changed"):
            evaluation.assert_live_corpus(corpus, store)
        assert len(store.snapshots()["items"]) == 1
