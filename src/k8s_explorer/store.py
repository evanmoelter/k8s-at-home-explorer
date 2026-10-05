"""Snapshot-pinned PostgreSQL indexes shared by independent retrieval families."""

import hashlib
import json
from collections import deque

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .extract import ALIASES
from .models import Extraction, Repository, Snapshot, SourceFile

DDL = """
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE TABLE IF NOT EXISTS index_metadata (key text PRIMARY KEY, value text NOT NULL);
INSERT INTO index_metadata VALUES ('schema_version','1') ON CONFLICT(key) DO UPDATE SET value=excluded.value;
CREATE TABLE IF NOT EXISTS repositories (
 id text PRIMARY KEY, name text NOT NULL, url text NOT NULL, branch text NOT NULL,
 stars integer NOT NULL, preference double precision NOT NULL, current_snapshot text
);
CREATE TABLE IF NOT EXISTS snapshots (
 id text PRIMARY KEY, repo_id text NOT NULL REFERENCES repositories(id), commit_sha text NOT NULL,
 fetched_at timestamptz NOT NULL, skipped jsonb NOT NULL DEFAULT '[]',
 semantic_providers text[] NOT NULL DEFAULT '{}'
);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS extraction_hash text;
CREATE TABLE IF NOT EXISTS files (
 id text PRIMARY KEY, repo_id text NOT NULL REFERENCES repositories(id),
 snapshot_id text NOT NULL REFERENCES snapshots(id), path text NOT NULL, payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
 id text PRIMARY KEY, repo_id text NOT NULL REFERENCES repositories(id),
 snapshot_id text NOT NULL REFERENCES snapshots(id), file_id text NOT NULL REFERENCES files(id),
 kind text NOT NULL, app text NOT NULL, services text[] NOT NULL, payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
 id text PRIMARY KEY, repo_id text NOT NULL REFERENCES repositories(id),
 snapshot_id text NOT NULL REFERENCES snapshots(id), source_id text NOT NULL REFERENCES resources(id),
 target_id text REFERENCES resources(id), relation text NOT NULL, payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
 id text PRIMARY KEY, repo_id text NOT NULL REFERENCES repositories(id),
 snapshot_id text NOT NULL REFERENCES snapshots(id), file_id text NOT NULL REFERENCES files(id),
 content_hash text NOT NULL, payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS embedding_cache (
 provider text NOT NULL, content_hash text NOT NULL, dimensions integer NOT NULL,
 embedding vector NOT NULL, PRIMARY KEY (provider,content_hash)
);
CREATE INDEX IF NOT EXISTS resources_snapshot ON resources(snapshot_id);
CREATE INDEX IF NOT EXISTS resources_services ON resources USING gin(services);
CREATE INDEX IF NOT EXISTS resources_kind_app ON resources(kind,app);
CREATE INDEX IF NOT EXISTS edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS chunks_snapshot ON chunks(snapshot_id);
"""


def _bounds(limit, offset=0):
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(offset) is not int
        or not 0 <= offset <= 100_000
    ):
        raise ValueError("limit must be 1..100 and offset 0..100000")


def _vector(values):
    return "[" + ",".join(str(v) for v in values) + "]"


class IndexStore:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def _connect(self):
        return psycopg.connect(
            self.database_url,
            row_factory=dict_row,
            connect_timeout=10,
            options="-c statement_timeout=30000 -c lock_timeout=10000",
        )

    def initialize(self):
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(884792173)")
            conn.execute(DDL)

    def publish(self, repo: Repository, snapshot: Snapshot, files: list[SourceFile], extraction: Extraction):
        if snapshot.repo_id != repo.id:
            raise ValueError("Snapshot repository mismatch")
        for item in [*files, *extraction.resources, *extraction.edges, *extraction.chunks]:
            if item.repo_id != repo.id or item.snapshot_id != snapshot.id:
                raise ValueError("Index item snapshot mismatch")
        file_ids = {item.id for item in files}
        resource_ids = {item.id for item in extraction.resources}
        for item in [*extraction.resources, *extraction.chunks]:
            if item.file_id not in file_ids:
                raise ValueError("Index item references a file outside the snapshot")
        for edge in extraction.edges:
            if edge.source_id not in resource_ids or (edge.target_id and edge.target_id not in resource_ids):
                raise ValueError("Edge endpoint outside the snapshot")
        for chunk in extraction.chunks:
            if chunk.resource_id and chunk.resource_id not in resource_ids:
                raise ValueError("Chunk resource outside the snapshot")
        extraction_hash = hashlib.sha256(
            json.dumps(extraction.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (repo.id,))
            conn.execute(
                """INSERT INTO repositories(id,name,url,branch,stars,preference) VALUES(%s,%s,%s,%s,%s,%s)
              ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,branch=excluded.branch,
              stars=excluded.stars,preference=excluded.preference""",
                (repo.id, repo.name, repo.url, repo.branch, repo.stars, repo.preference),
            )
            existing = conn.execute("SELECT * FROM snapshots WHERE id=%s", (snapshot.id,)).fetchone()
            if existing and (existing["repo_id"] != repo.id or existing["commit_sha"] != snapshot.commit):
                raise ValueError("Snapshot identity conflict")
            rebuild = not existing or existing["extraction_hash"] != extraction_hash
            if existing and rebuild:
                for table in ("edges", "chunks", "resources", "files"):
                    conn.execute(f"DELETE FROM {table} WHERE snapshot_id=%s", (snapshot.id,))
            if not existing:
                conn.execute(
                    "INSERT INTO snapshots(id,repo_id,commit_sha,fetched_at,skipped) VALUES(%s,%s,%s,%s,%s)",
                    (snapshot.id, repo.id, snapshot.commit, snapshot.fetched_at, Jsonb(extraction.skipped)),
                )
            if rebuild:
                conn.execute(
                    "UPDATE snapshots SET extraction_hash=%s,skipped=%s,semantic_providers='{}' WHERE id=%s",
                    (extraction_hash, Jsonb(extraction.skipped), snapshot.id),
                )
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO files VALUES(%s,%s,%s,%s,%s)",
                        [(f.id, f.repo_id, f.snapshot_id, f.path, Jsonb(f.model_dump())) for f in files],
                    )
                    cur.executemany(
                        "INSERT INTO resources VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                r.id,
                                r.repo_id,
                                r.snapshot_id,
                                r.file_id,
                                r.kind,
                                r.app,
                                r.services,
                                Jsonb(r.model_dump()),
                            )
                            for r in extraction.resources
                        ],
                    )
                    cur.executemany(
                        "INSERT INTO edges VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                e.id,
                                e.repo_id,
                                e.snapshot_id,
                                e.source_id,
                                e.target_id,
                                e.relation,
                                Jsonb(e.model_dump()),
                            )
                            for e in extraction.edges
                        ],
                    )
                    cur.executemany(
                        "INSERT INTO chunks VALUES(%s,%s,%s,%s,%s,%s)",
                        [
                            (c.id, c.repo_id, c.snapshot_id, c.file_id, c.content_hash, Jsonb(c.model_dump()))
                            for c in extraction.chunks
                        ],
                    )
            conn.execute("UPDATE repositories SET current_snapshot=%s WHERE id=%s", (snapshot.id, repo.id))
            # The replacement becomes visible together with removal of prior data.
            # Failure anywhere in this transaction preserves the previous index.
            for table in ("edges", "chunks", "resources", "files"):
                conn.execute(
                    f"DELETE FROM {table} WHERE repo_id=%s AND snapshot_id<>%s", (repo.id, snapshot.id)
                )
            conn.execute("DELETE FROM snapshots WHERE repo_id=%s AND id<>%s", (repo.id, snapshot.id))
            conn.execute("""DELETE FROM embedding_cache v WHERE NOT EXISTS (
                SELECT 1 FROM chunks c JOIN repositories p ON p.id=c.repo_id
                WHERE c.snapshot_id=p.current_snapshot AND c.content_hash=v.content_hash)""")

    def _query(self, query, params=()):
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def health(self):
        metadata = self._query("SELECT value FROM index_metadata WHERE key='schema_version'")
        if not metadata or metadata[0]["value"] != "1":
            raise ValueError("Index schema is not initialized")
        return {"status": "ok", "backend": "postgresql", "schema_version": "1"}

    def schema(self):
        return {
            "families": {
                "structured": ["repositories with latest scan metadata", "resources", "aggregates"],
                "graph": ["resources as nodes", "explicit or unresolved edges"],
                "semantic": ["chunks", "provider-specific cosine similarity"],
            },
            "resource_fields": ["kind", "name", "namespace", "app", "services", "images", "context"],
            "relations": [
                "sourceRef",
                "chartRef",
                "dependsOn",
                "secretKeyRef",
                "secretRef",
                "configMapKeyRef",
                "configMapRef",
                "backendRefs",
                "claimName",
                "resources",
                "components",
            ],
            "service_aliases": ALIASES,
            "defaults": "Latest indexed state only; snapshot_id can assert the expected current scan",
            "coverage": "Raw manifests; references requiring rendering remain unresolved",
        }

    def repositories(self, services=None, app=None, sort="stars", limit=20, offset=0):
        _bounds(limit, offset)
        ordering = {
            "stars": "p.stars DESC,p.id",
            "preference": "p.preference DESC,p.stars DESC,p.id",
            "name": "p.name,p.id",
        }
        if sort not in ordering:
            raise ValueError("sort must be stars, preference, or name")
        clauses, params = ["p.current_snapshot IS NOT NULL"], []
        if services:
            canonical = [ALIASES.get(s, s) for s in services]
            clauses.append("""EXISTS (SELECT 1 FROM resources r
                LEFT JOIN LATERAL unnest(r.services) AS svc ON true
                WHERE r.snapshot_id=p.current_snapshot AND r.payload->>'context'<>'unknown'
                GROUP BY r.payload->>'context' HAVING %s::text[] <@ array_agg(DISTINCT svc))""")
            params.append(canonical)
            if app:
                clauses[-1] = clauses[-1].replace(
                    "array_agg(DISTINCT svc))", "array_agg(DISTINCT svc) AND bool_or(r.app=%s))"
                )
                params.append(app)
        if app:
            clauses.append(
                "EXISTS(SELECT 1 FROM resources r WHERE r.snapshot_id=p.current_snapshot AND r.app=%s)"
            )
            params.append(app)
        query = """SELECT p.*, s.commit_sha AS commit, s.fetched_at,s.semantic_providers,s.extraction_hash,
           'indexed'::text AS status,jsonb_array_length(s.skipped) AS skipped_count,
           jsonb_array_length(s.skipped)>20 AS skipped_truncated,
           jsonb_path_query_array(s.skipped,'$[0 to 19]') AS skipped,
           ARRAY(SELECT DISTINCT unnest(r.services) FROM resources r WHERE
             r.snapshot_id=p.current_snapshot) AS services
           FROM repositories p JOIN snapshots s ON s.id=p.current_snapshot WHERE """
        with self._connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = conn.execute(
                query + " AND ".join(clauses) + " ORDER BY " + ordering[sort] + " LIMIT %s OFFSET %s",
                (*params, limit + 1, offset),
            ).fetchall()
            for row in rows:
                contexts = conn.execute(
                    """SELECT payload->>'context' AS context,
                  array_agg(DISTINCT svc) AS services,array_agg(DISTINCT app) AS apps FROM resources
                  LEFT JOIN LATERAL unnest(services) AS svc ON true
                  WHERE snapshot_id=%s GROUP BY payload->>'context' ORDER BY context""",
                    (row["current_snapshot"],),
                ).fetchall()
                row["contexts"] = [
                    c
                    for c in contexts
                    if (
                        not services or (c["context"] != "unknown" and set(canonical).issubset(c["services"]))
                    )
                    and (not app or app in c["apps"])
                ]
                for context in row["contexts"]:
                    context["app_count"] = len(context.pop("apps"))
                    context["services"] = [service for service in context["services"] if service is not None]
        return self._page(rows, limit, offset, note="Services indicate context presence, not app integration")

    def _filters(self, alias, repo_ids=None, snapshot_id=None):
        clauses, params = [f"{alias}.snapshot_id=p.current_snapshot"], []
        if repo_ids:
            if len(repo_ids) > 100:
                raise ValueError("Too many repository IDs")
            clauses.append(f"{alias}.repo_id = ANY(%s)")
            params.append(repo_ids)
        if snapshot_id:
            clauses.append(f"{alias}.snapshot_id=%s")
            params.append(snapshot_id)
        return clauses, params

    def resources(
        self,
        repo_ids=None,
        snapshot_id=None,
        kind=None,
        app=None,
        service=None,
        text=None,
        limit=20,
        offset=0,
        context=None,
        namespace=None,
    ):
        _bounds(limit, offset)
        clauses, params = self._filters("r", repo_ids, snapshot_id)
        for column, value in (("kind", kind), ("app", app)):
            if value:
                clauses.append(f"r.{column}=%s")
                params.append(value)
        for field, value in (("context", context), ("namespace", namespace)):
            if value is not None:
                clauses.append(f"r.payload->>'{field}'=%s")
                params.append(value)
        if service:
            clauses.append("%s=ANY(r.services)")
            params.append(ALIASES.get(service, service))
        if text:
            if len(text) > 1000:
                raise ValueError("Text query exceeds budget")
            clauses.append("to_tsvector('simple',r.payload::text) @@ plainto_tsquery('simple',%s)")
            params.append(text)
        rows = self._query(
            """SELECT r.payload-'document' AS payload,s.commit_sha AS commit,p.url AS repo_url
                            FROM resources r JOIN repositories p ON p.id=r.repo_id
                            JOIN snapshots s ON s.id=r.snapshot_id WHERE """
            + " AND ".join(clauses)
            + " ORDER BY p.stars DESC,r.id LIMIT %s OFFSET %s",
            (*params, limit + 1, offset),
        )
        summaries = []
        for row in rows:
            summary = self._evidence(row)
            summary.pop("document", None)
            summaries.append(summary)
        return self._page(summaries, limit, offset, note="Read individual resource IDs for full documents")

    def resource(self, resource_id):
        rows = self._query(
            """SELECT r.payload,s.commit_sha AS commit,p.url AS repo_url FROM resources r
           JOIN snapshots s ON s.id=r.snapshot_id JOIN repositories p ON p.id=r.repo_id
           WHERE r.id=%s AND r.snapshot_id=p.current_snapshot""",
            (resource_id,),
        )
        return self._evidence(rows[0]) if rows else {"error": "resource_not_found"}

    def snapshots(self, repo_id=None, limit=20):
        _bounds(limit)
        rows = self._query(
            "SELECT s.* FROM snapshots s JOIN repositories p ON p.id=s.repo_id WHERE s.id=p.current_snapshot"
            + (" AND s.repo_id=%s" if repo_id else "")
            + " ORDER BY s.fetched_at DESC,s.id LIMIT %s",
            (repo_id, limit) if repo_id else (limit,),
        )
        for row in rows:
            row["commit"] = row.pop("commit_sha")
            skipped = row["skipped"]
            row["skipped_count"] = len(skipped)
            row["skipped_truncated"] = len(skipped) > 20
            row["skipped"] = skipped[:20]
        return {"items": rows}

    def current_snapshot(self, repo_id):
        snapshots = self.snapshots(repo_id=repo_id, limit=1)["items"]
        if not snapshots:
            raise ValueError("Repository has no indexed scan")
        return {**snapshots[0], "status": "indexed"}

    def graph_nodes(self, **kwargs):
        return self.resources(**kwargs)

    def neighbors(self, node_id, relation=None, direction="out", limit=20):
        _bounds(limit)
        if direction not in {"in", "out", "both"}:
            raise ValueError("direction must be in, out, or both")
        clause = {
            "in": "e.target_id=%s",
            "out": "e.source_id=%s",
            "both": "(e.source_id=%s OR e.target_id=%s)",
        }[direction]
        clause += " AND e.snapshot_id=p.current_snapshot"
        params = [node_id, node_id] if direction == "both" else [node_id]
        if relation:
            clause += " AND e.relation=%s"
            params.append(relation)
        rows = self._query(
            """SELECT e.payload,s.commit_sha AS commit,p.url AS repo_url FROM edges e
                            JOIN snapshots s ON s.id=e.snapshot_id
                            JOIN repositories p ON p.id=e.repo_id WHERE """
            + clause
            + " ORDER BY e.id LIMIT %s",
            (*params, limit + 1),
        )
        return self._page([self._evidence(r) for r in rows], limit, 0)

    def paths(self, source_id, target_id, max_depth=3, max_nodes=100):
        if (
            type(max_depth) is not int
            or not 1 <= max_depth <= 6
            or type(max_nodes) is not int
            or not 1 <= max_nodes <= 1000
        ):
            raise ValueError("Traversal budgets: depth 1..6, nodes 1..1000")
        source, target = self.resource(source_id), self.resource(target_id)
        if "error" in source or "error" in target:
            return {"paths": [], "error": "resource_not_found"}
        if source["snapshot_id"] != target["snapshot_id"]:
            return {"paths": [], "error": "different_snapshots"}
        queue, visited = deque([(source_id, [])]), {source_id}
        truncated = False
        while queue:
            node, path = queue.popleft()
            if node == target_id:
                return {
                    "paths": [path],
                    "visited_nodes": len(visited),
                    "truncated": truncated,
                    "snapshot_id": source["snapshot_id"],
                    "direction": "out",
                }
            if len(path) >= max_depth:
                continue
            remaining = max_nodes - len(visited)
            if remaining <= 0:
                truncated = True
                continue
            edges = self._query(
                "SELECT e.payload FROM edges e JOIN repositories p ON p.id=e.repo_id "
                "WHERE e.source_id=%s AND e.snapshot_id=p.current_snapshot AND e.target_id IS NOT NULL "
                "ORDER BY e.id LIMIT %s",
                (node, remaining + 1),
            )
            truncated |= len(edges) > remaining
            for row in edges[:remaining]:
                edge = row["payload"]
                if edge["target_id"] not in visited:
                    visited.add(edge["target_id"])
                    queue.append((edge["target_id"], [*path, edge]))
        return {
            "paths": [],
            "visited_nodes": len(visited),
            "truncated": truncated,
            "snapshot_id": source["snapshot_id"],
            "direction": "out",
        }

    def aggregates(self, field="app", repo_ids=None, limit=20):
        _bounds(limit)
        if field not in {"app", "kind", "service", "image"}:
            raise ValueError("field must be app, kind, service, or image")
        clauses, params = self._filters("r", repo_ids)
        if field == "app":
            clauses.append("r.kind=ANY(%s)")
            params.append(
                ["HelmRelease", "Application", "Deployment", "StatefulSet", "DaemonSet", "Pod", "CronJob"]
            )
        expr = {
            "service": "unnest(r.services)",
            "image": "jsonb_array_elements_text(r.payload->'images')",
        }.get(field, f"r.{field}")
        rows = self._query(
            "SELECT value,count(DISTINCT repo_id) AS repositories,count(*) AS resources FROM "
            f"(SELECT {expr} AS value,r.repo_id FROM resources r JOIN repositories p ON p.id=r.repo_id WHERE "
            + " AND ".join(clauses)
            + ") t WHERE value<>'' GROUP BY value "
            "ORDER BY repositories DESC,value LIMIT %s",
            (*params, limit),
        )
        return {
            "field": field,
            "items": rows,
            "scope": "Distinct current repositories; forks not deduplicated",
        }

    @staticmethod
    def _evidence(row):
        result = dict(row["payload"])
        result["commit"] = row["commit"]
        result["repo_url"] = row["repo_url"]
        if "path" in result:
            result["source"] = {
                "file_id": result.get("file_id"),
                "path": result["path"],
                "start_line": result.get("start_line"),
                "end_line": result.get("end_line"),
            }
        return result

    @staticmethod
    def _page(rows, limit, offset, **extra):
        return {
            "items": rows[:limit],
            "has_more": len(rows) > limit,
            "next_offset": offset + limit if len(rows) > limit else None,
            **extra,
        }

    def index_embeddings(self, snapshot_id, provider):
        snapshots = self._query(
            "SELECT s.repo_id,s.extraction_hash FROM snapshots s JOIN repositories p ON p.id=s.repo_id "
            "WHERE s.id=%s AND s.id=p.current_snapshot",
            (snapshot_id,),
        )
        if not snapshots:
            raise ValueError("Snapshot not found")
        captured = snapshots[0]
        chunks = self._query(
            "SELECT content_hash,payload FROM chunks WHERE snapshot_id=%s ORDER BY id", (snapshot_id,)
        )
        unique = {r["content_hash"]: r["payload"]["content"] for r in chunks}
        cached = self._query(
            "SELECT content_hash FROM embedding_cache WHERE provider=%s AND content_hash=ANY(%s)",
            (provider.cache_key, list(unique)),
        )
        cached_hashes = {row["content_hash"] for row in cached}
        missing = [(key, text) for key, text in unique.items() if key not in cached_hashes]
        for offset in range(0, len(missing), 16):
            batch = missing[offset : offset + 16]
            vectors = provider.embed_documents([text for _, text in batch])
            with self._connect() as conn:
                # A replacement may finish while the provider request is in flight.
                # Do not insert obsolete vectors after that publication's pruning.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (captured["repo_id"],))
                active = conn.execute(
                    "SELECT s.extraction_hash FROM snapshots s JOIN repositories p ON p.id=s.repo_id "
                    "WHERE s.id=%s AND s.id=p.current_snapshot",
                    (snapshot_id,),
                ).fetchone()
                if not active or active["extraction_hash"] != captured["extraction_hash"]:
                    raise ValueError("Snapshot extraction changed during embedding indexing; retry")
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO embedding_cache VALUES(%s,%s,%s,%s::vector) ON CONFLICT DO NOTHING",
                        [
                            (provider.cache_key, key, provider.dimensions, _vector(vector))
                            for (key, _), vector in zip(batch, vectors, strict=True)
                        ],
                    )
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (captured["repo_id"],))
            current = conn.execute(
                "SELECT extraction_hash FROM snapshots WHERE id=%s", (snapshot_id,)
            ).fetchone()
            if not current or current["extraction_hash"] != captured["extraction_hash"]:
                raise ValueError("Snapshot extraction changed during embedding indexing; retry")
            missing_count = conn.execute(
                """SELECT count(*) AS count FROM chunks c WHERE c.snapshot_id=%s AND NOT EXISTS (
                SELECT 1 FROM embedding_cache v WHERE v.content_hash=c.content_hash AND v.provider=%s
                AND v.dimensions=%s)
                """,
                (snapshot_id, provider.cache_key, provider.dimensions),
            ).fetchone()["count"]
            if missing_count:
                raise ValueError("Snapshot embedding indexing is incomplete")
            conn.execute(
                "UPDATE snapshots SET semantic_providers=array_append(semantic_providers,%s) "
                "WHERE id=%s AND NOT %s=ANY(semantic_providers)",
                (provider.cache_key, snapshot_id, provider.cache_key),
            )
        return {
            "snapshot_id": snapshot_id,
            "chunks": len(chunks),
            "embedded": len(missing),
            "provider": provider.cache_key,
            "model": provider.model,
        }

    def _semantic(
        self,
        vector,
        provider,
        repo_ids=None,
        snapshot_id=None,
        limit=20,
        exclude=None,
        query=None,
        chunk_ids=None,
    ):
        _bounds(limit)
        readiness_clauses, readiness_params = self._filters("s", repo_ids, snapshot_id)
        # Snapshot rows use id rather than the snapshot_id field on child tables.
        readiness_clauses = [clause.replace("s.snapshot_id", "s.id") for clause in readiness_clauses]
        readiness_query = (
            "SELECT count(*) AS selected,count(*) FILTER (WHERE %s=ANY(s.semantic_providers)) AS ready "
            ",jsonb_agg(jsonb_build_array(s.id,s.extraction_hash,%s=ANY(s.semantic_providers)) "
            "ORDER BY s.id) AS scans FROM snapshots s JOIN repositories p ON p.id=s.repo_id WHERE "
            + " AND ".join(readiness_clauses)
        )
        readiness_arguments = (provider.cache_key, provider.cache_key, *readiness_params)
        readiness = self._query(readiness_query, readiness_arguments)[0]
        if not readiness["ready"]:
            return {
                "available": False,
                "items": [],
                "reason": "No matching snapshots are ready for this embedding provider",
                "selected_snapshots": readiness["selected"],
                "ready_snapshots": 0,
                "provider": provider.cache_key,
                "model": provider.model,
            }
        if vector is None:
            vector = provider.embed_query(query)
        clauses, params = self._filters("c", repo_ids, snapshot_id)
        clauses.extend(["v.provider=%s", "v.dimensions=%s", "%s=ANY(s.semantic_providers)"])
        params.extend([provider.cache_key, provider.dimensions, provider.cache_key])
        if chunk_ids is not None:
            clauses.append("c.id=ANY(%s)")
            params.append(chunk_ids)
        if exclude:
            clauses.append("c.id<>%s")
            params.append(exclude)
        with self._connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            current = conn.execute(readiness_query, readiness_arguments).fetchone()
            if current["scans"] != readiness["scans"]:
                return {
                    "available": False,
                    "reason": "scan_changed",
                    "items": [],
                    "selected_snapshots": current["selected"],
                    "ready_snapshots": current["ready"],
                    "provider": provider.cache_key,
                    "model": provider.model,
                    "hint": "Repository scan changed; refresh query results and retry",
                }
            rows = conn.execute(
                """SELECT c.payload,s.commit_sha AS commit,p.url AS repo_url,
                 1-(v.embedding <=> %s::vector) AS cosine_similarity FROM chunks c
                 JOIN repositories p ON p.id=c.repo_id JOIN snapshots s ON s.id=c.snapshot_id
                 JOIN embedding_cache v ON v.content_hash=c.content_hash WHERE """
                + " AND ".join(clauses)
                + " ORDER BY v.embedding <=> %s::vector,c.id LIMIT %s",
                (_vector(vector), *params, _vector(vector), limit + 1),
            ).fetchall()
        results = []
        for row in rows:
            item = self._evidence(row)
            item["cosine_similarity"] = row["cosine_similarity"]
            results.append(item)
        return self._page(
            results,
            limit,
            0,
            model=provider.model,
            provider=provider.cache_key,
            available=True,
            selected_snapshots=readiness["selected"],
            ready_snapshots=readiness["ready"],
            note="Only snapshots fully indexed with this provider are searched",
        )

    def semantic_search(self, query, provider, repo_ids=None, snapshot_id=None, limit=20, chunk_ids=None):
        if not isinstance(query, str) or not query.strip() or len(query.encode()) > 32_000:
            raise ValueError("Semantic query must contain 1..32000 bytes")
        return self._semantic(None, provider, repo_ids, snapshot_id, limit, query=query, chunk_ids=chunk_ids)

    def similar_chunks(self, chunk_id, provider, repo_ids=None, snapshot_id=None, limit=20):
        rows = self._query(
            """SELECT v.embedding::text AS vector FROM chunks c JOIN embedding_cache v
              ON c.content_hash=v.content_hash JOIN repositories p ON p.id=c.repo_id
              WHERE c.id=%s AND c.snapshot_id=p.current_snapshot AND v.provider=%s AND v.dimensions=%s""",
            (chunk_id, provider.cache_key, provider.dimensions),
        )
        if not rows:
            return {"error": "chunk_embedding_unavailable", "items": []}
        vector = json.loads(rows[0]["vector"])
        return self._semantic(vector, provider, repo_ids, snapshot_id, limit, exclude=chunk_id)
