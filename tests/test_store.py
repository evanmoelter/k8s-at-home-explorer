"""Real disposable PostgreSQL tests; never target a production database."""

import os
import uuid

import psycopg
import pytest
from psycopg import sql

from k8s_explorer.extract import extract
from k8s_explorer.models import Repository, Snapshot, SourceFile
from k8s_explorer.store import IndexStore


@pytest.fixture
def store():
    url = os.environ.get("EXPLORER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set EXPLORER_TEST_DATABASE_URL to a disposable PostgreSQL + pgvector instance")
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    instance = IndexStore(url)
    connect = instance._connect

    def connection():
        conn = connect()
        conn.execute(sql.SQL("SET search_path TO {},public").format(sql.Identifier(schema)))
        return conn

    instance._connect = connection
    instance.initialize()
    yield instance
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def publish(store, repo_id="repo", stars=1, cluster="apollo", app="demo", commit="a" * 40):
    repo = Repository(id=repo_id, name=repo_id, url=f"https://github.com/example/{repo_id}", stars=stars)
    snap = Snapshot(id=f"{repo_id}-{commit}", repo_id=repo_id, commit=commit)
    text = f"""apiVersion: v1
kind: HelmRelease
metadata:
  name: {app}
  namespace: apps
spec:
  dependsOn:
    - name: database
---
apiVersion: postgresql.cnpg.io/v1
kind: HelmRelease
metadata:
  name: database
  namespace: apps
"""
    file = SourceFile(
        id=snap.id + "-file",
        repo_id=repo_id,
        snapshot_id=snap.id,
        path=f"kubernetes/{cluster}/apps/demo.yaml",
        blob="b" * 40,
        size=len(text),
    )
    extraction = extract(snap, [(file, text)])
    store.publish(repo, snap, [file], extraction)
    return repo, snap, file, extraction


@pytest.mark.integration
def test_search_graph_snapshot_and_atomicity(store):
    repo, snapshot, file, extracted = publish(store)
    assert store.repositories(services=["cnpg"])["items"][0]["id"] == repo.id
    assert store.resources(app="demo", namespace="apps")["items"][0]["commit"] == snapshot.commit
    assert store.neighbors(extracted.resources[0].id)["items"][0]["target_id"] == extracted.resources[1].id
    assert store.paths(extracted.resources[0].id, extracted.resources[1].id)["paths"]
    assert store.aggregates(field="service")["items"][0]["value"] == "cloudnative-pg"
    publish(store, app="next", commit="c" * 40)
    assert store.resources(app="demo")["items"] == []
    assert store.resources(app="demo", snapshot_id=snapshot.id)["items"] == []
    assert store.resource(extracted.resources[0].id)["error"] == "resource_not_found"
    assert store.neighbors(extracted.resources[0].id)["items"] == []
    assert [entry["commit"] for entry in store.snapshots(repo.id)["items"]] == ["c" * 40]
    # Deliberate duplicate resource makes the entire publication roll back.
    repo2, snap2, file2, extracted2 = publish(store, repo_id="second")
    snap2.id = "bad-snapshot"
    file2.snapshot_id = snap2.id
    file2.id = "bad-file"
    for item in [*extracted2.resources, *extracted2.edges, *extracted2.chunks]:
        item.snapshot_id = snap2.id
        if hasattr(item, "file_id"):
            item.file_id = file2.id
    with pytest.raises(psycopg.IntegrityError):
        store.publish(repo2, snap2, [file2], extracted2)
    assert not store._query("SELECT id FROM snapshots WHERE id=%s", (snap2.id,))


@pytest.mark.integration
def test_stars_preferences_and_combination_context(store):
    publish(store, repo_id="low", stars=2)
    publish(store, repo_id="high", stars=20)
    assert [r["id"] for r in store.repositories(limit=1)["items"]] == ["high"]
    assert store.repositories(limit=1)["has_more"]
    assert store.repositories(services=["cnpg", "cilium"])["items"] == []
    assert store.resources(context="kubernetes/wrong")["items"] == []
    with pytest.raises(ValueError):
        store.resources(limit=1000)


@pytest.mark.integration
def test_reindex_same_snapshot_and_validate_endpoints(store):
    repo, snap, file, extracted = publish(store)
    resource = extracted.resources[0]
    resource.app = "updated-parser-output"
    store._query(
        "UPDATE snapshots SET semantic_providers=ARRAY['old-provider'] WHERE id=%s RETURNING id", (snap.id,)
    )
    store.publish(repo, snap, [file], extracted)
    assert store.resources(app="updated-parser-output")["items"]
    row = store._query("SELECT extraction_hash,semantic_providers FROM snapshots WHERE id=%s", (snap.id,))[0]
    assert row["extraction_hash"]
    assert row["semantic_providers"] == []
    extracted.edges[0].target_id = "outside-snapshot"
    with pytest.raises(ValueError, match="endpoint"):
        store.publish(repo, snap, [file], extracted)


@pytest.mark.integration
def test_app_and_services_share_context(store):
    repo, snap, file, extracted = publish(store)
    assert store.repositories(services=["cnpg"], app="demo")["items"]
    other_text = "apiVersion: v1\nkind: Pod\nmetadata:\n  name: distant\n  namespace: apps\n"
    other_file = SourceFile(
        id="other-file",
        repo_id=repo.id,
        snapshot_id=snap.id,
        path="kubernetes/other/apps/pod.yaml",
        blob="c",
        size=len(other_text),
    )
    other = extract(snap, [(other_file, other_text)])
    extracted.resources.extend(other.resources)
    extracted.chunks.extend(other.chunks)
    store.publish(repo, snap, [file, other_file], extracted)
    assert store.repositories(services=["cnpg"], app="distant")["items"] == []
    assert store.health()["schema_version"] == "1"
    assert store.aggregates(field="image")["items"] == []


@pytest.mark.integration
def test_real_embedding_provider_and_cache(store, real_embedding_provider):
    provider = real_embedding_provider
    _, snap, _, extracted = publish(store)
    assert store.index_embeddings(snap.id, provider)["embedded"] == len(extracted.chunks)
    requests = provider.usage_stats["document_requests"]
    assert store.index_embeddings(snap.id, provider)["embedded"] == 0
    assert provider.usage_stats["document_requests"] == requests
    result = store.semantic_search(extracted.chunks[0].content, provider)
    assert result["available"] is True
    assert result["items"] and result["ready_snapshots"] == result["selected_snapshots"] == 1
    assert {item["id"] for item in result["items"]} <= {chunk.id for chunk in extracted.chunks}
    assert store.similar_chunks(extracted.chunks[0].id, provider)["items"][0]["id"] != extracted.chunks[0].id


@pytest.mark.integration
def test_summary_documents_and_skipped_budget(store):
    repo, snap, file, extracted = publish(store)
    extracted.skipped = [{"path": f"large-{index}.yaml", "reason": "too_large"} for index in range(100)]
    store.publish(repo, snap, [file], extracted)
    assert "document" not in store.resources()["items"][0]
    assert "document" not in store.graph_nodes()["items"][0]
    assert "document" in store.resource(extracted.resources[0].id)
    metadata = store.snapshots()["items"][0]
    assert metadata["skipped_count"] == 100
    assert len(metadata["skipped"]) == 20
    assert metadata["skipped_truncated"]


@pytest.mark.integration
def test_pgvector_sql_cosine_provider_filters_and_readiness(store):
    """Known numeric SQL fixtures test vector arithmetic, not semantic retrieval quality.

    No fake provider generates embeddings; production indexing always calls a real provider.
    """
    from types import SimpleNamespace

    repo, snap, file, extracted = publish(store)
    provider = SimpleNamespace(cache_key="numeric-sql-fixture", model="numeric-sql-fixture", dimensions=2)
    vectors = ("[1,0]", "[0,1]")
    with store._connect() as connection:
        for chunk, vector in zip(extracted.chunks, vectors, strict=True):
            connection.execute(
                "INSERT INTO embedding_cache VALUES(%s,%s,%s,%s::vector)",
                (provider.cache_key, chunk.content_hash, provider.dimensions, vector),
            )
        connection.execute(
            "UPDATE snapshots SET semantic_providers=ARRAY[%s] WHERE id=%s", (provider.cache_key, snap.id)
        )
    result = store._semantic([1, 0], provider, snapshot_id=snap.id)
    assert result["items"][0]["id"] == extracted.chunks[0].id
    assert result["items"][0]["cosine_similarity"] == pytest.approx(1)
    assert result["items"][1]["cosine_similarity"] == pytest.approx(0)
    assert store._semantic([1, 0], provider, repo_ids=["absent"])["items"] == []
    scoped = store._semantic([1, 0], provider, chunk_ids=[extracted.chunks[1].id])
    assert [row["id"] for row in scoped["items"]] == [extracted.chunks[1].id]
    assert scoped["items"][0]["cosine_similarity"] == pytest.approx(0)
    assert store._semantic([1, 0], provider, chunk_ids=[])["items"] == []
    missing_provider = SimpleNamespace(cache_key="absent-provider", model="absent", dimensions=2)
    assert store._semantic([1, 0], missing_provider)["items"] == []
    similar = store.similar_chunks(extracted.chunks[0].id, provider, snapshot_id=snap.id)
    assert similar["items"][0]["id"] == extracted.chunks[1].id
    assert similar["items"][0]["cosine_similarity"] == pytest.approx(0)
    extracted.resources[0].app = "new-parser-version"
    store.publish(repo, snap, [file], extracted)
    assert store._semantic([1, 0], provider, snapshot_id=snap.id)["items"] == []


@pytest.mark.integration
def test_embedding_index_does_not_publish_stale_readiness(store, monkeypatch):
    from types import SimpleNamespace

    repo, snap, file, extracted = publish(store)
    provider = SimpleNamespace(cache_key="cached-numeric-fixture", model="numeric-fixture", dimensions=2)
    with store._connect() as connection:
        for chunk in extracted.chunks:
            connection.execute(
                "INSERT INTO embedding_cache VALUES(%s,%s,%s,%s::vector)",
                (provider.cache_key, chunk.content_hash, 2, "[1,0]"),
            )
    query = store._query

    def concurrent_reindex(statement, params=()):
        rows = query(statement, params)
        if statement.startswith("SELECT content_hash FROM embedding_cache"):
            extracted.resources[0].app = "changed-concurrently"
            store.publish(repo, snap, [file], extracted)
        return rows

    monkeypatch.setattr(store, "_query", concurrent_reindex)
    with pytest.raises(ValueError, match="changed during"):
        store.index_embeddings(snap.id, provider)
    assert (
        query("SELECT semantic_providers FROM snapshots WHERE id=%s", (snap.id,))[0]["semantic_providers"]
        == []
    )


@pytest.mark.integration
def test_store_calls_explicit_document_and_query_roles_without_legacy_fallback(store):
    """Fail-before-vector adapters prove role routing without fabricating embeddings."""
    from types import SimpleNamespace

    _, snap, _, _ = publish(store)

    def legacy(*arguments):
        pytest.fail("Legacy embed method must not choose retrieval roles")

    def documents(texts):
        assert isinstance(texts, list) and texts
        raise ValueError("document-role-observed")

    def query(text):
        assert text == "backup scheduling"
        raise ValueError("query-role-observed")

    provider = SimpleNamespace(
        cache_key="role-routing",
        model="role-routing",
        dimensions=2,
        embed=legacy,
        embed_documents=documents,
        embed_query=query,
    )
    with pytest.raises(ValueError, match="document-role-observed"):
        store.index_embeddings(snap.id, provider)
    with store._connect() as connection:
        connection.execute(
            "UPDATE snapshots SET semantic_providers=ARRAY[%s] WHERE id=%s", (provider.cache_key, snap.id)
        )
    with pytest.raises(ValueError, match="query-role-observed"):
        store.semantic_search("backup scheduling", provider)


@pytest.mark.integration
def test_semantic_unready_snapshot_avoids_provider_request(store):
    from types import SimpleNamespace

    _, snap, _, _ = publish(store)
    # Intentionally has no embed() method: unavailable snapshots need no network call.
    provider = SimpleNamespace(cache_key="unready-fixture", model="unready", dimensions=2)
    result = store.semantic_search("backup configuration", provider, snapshot_id=snap.id)
    assert result["available"] is False
    assert result["selected_snapshots"] == 1


@pytest.mark.integration
def test_app_aggregation_counts_only_deployments(store):
    repo, snap, file, extracted = publish(store)
    extra_text = (
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        "resources: []\n---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: config}\n"
    )
    extra_file = SourceFile(
        id="config-file",
        repo_id=repo.id,
        snapshot_id=snap.id,
        path="kubernetes/apollo/apps/app/kustomization.yaml",
        blob="c",
        size=len(extra_text),
    )
    extra = extract(snap, [(extra_file, extra_text)])
    extracted.resources.extend(extra.resources)
    extracted.chunks.extend(extra.chunks)
    store.publish(repo, snap, [file, extra_file], extracted)
    apps = {row["value"] for row in store.aggregates(field="app")["items"]}
    assert apps == {"demo", "database"}
    kinds = {row["value"] for row in store.aggregates(field="kind")["items"]}
    assert "Kustomization" in kinds
    assert "ConfigMap" in kinds


@pytest.mark.integration
def test_latest_scan_replacement_deletion_and_stale_ids(store):
    from k8s_explorer.models import Extraction

    repo, previous, _, extracted = publish(store)
    old_resource, old_chunk = extracted.resources[0].id, extracted.chunks[0].id
    current = Snapshot(id="empty-current", repo_id=repo.id, commit="d" * 40)
    store.publish(repo, current, [], Extraction())
    assert store.current_snapshot(repo.id)["id"] == current.id
    assert store.current_snapshot(repo.id)["commit"] == current.commit
    assert store.current_snapshot(repo.id)["status"] == "indexed"
    assert store.resources(repo_ids=[repo.id])["items"] == []
    assert store.resources(snapshot_id=previous.id)["items"] == []
    assert store.resource(old_resource)["error"] == "resource_not_found"
    assert store.neighbors(old_resource)["items"] == []
    assert store.paths(old_resource, old_resource)["error"] == "resource_not_found"
    from types import SimpleNamespace

    provider = SimpleNamespace(cache_key="fixture", dimensions=2, model="fixture")
    assert store.similar_chunks(old_chunk, provider)["error"] == "chunk_embedding_unavailable"
    assert [row["id"] for row in store._query("SELECT id FROM snapshots WHERE repo_id=%s", (repo.id,))] == [
        current.id
    ]
    for table in ("files", "resources", "edges", "chunks"):
        assert store._query(f"SELECT count(*) AS n FROM {table} WHERE repo_id=%s", (repo.id,))[0]["n"] == 0
    with pytest.raises(ValueError, match="no indexed scan"):
        store.current_snapshot("missing")


@pytest.mark.integration
def test_failed_latest_publication_preserves_previous_scan_and_vectors(store):
    repo, previous, file, extracted = publish(store)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO embedding_cache VALUES(%s,%s,%s,%s::vector)",
            ("fixture", extracted.chunks[0].content_hash, 2, "[1,0]"),
        )
    new_snapshot = previous.model_copy(update={"id": "failing-current", "commit": "f" * 40})
    new_file = file.model_copy(update={"id": "failing-file", "snapshot_id": new_snapshot.id})
    invalid = extracted.model_copy(deep=True)
    for item in [*invalid.resources, *invalid.edges, *invalid.chunks]:
        item.snapshot_id = new_snapshot.id
        if hasattr(item, "file_id"):
            item.file_id = new_file.id
    # Deliberate reused primary IDs fail after beginning the publication transaction.
    with pytest.raises(psycopg.IntegrityError):
        store.publish(repo, new_snapshot, [new_file], invalid)
    assert store.current_snapshot(repo.id)["id"] == previous.id
    assert store.resource(extracted.resources[0].id)["id"] == extracted.resources[0].id
    assert store.neighbors(extracted.resources[0].id)["items"]
    assert not store._query("SELECT id FROM snapshots WHERE id=%s", (new_snapshot.id,))
    assert store._query("SELECT content_hash FROM embedding_cache WHERE provider='fixture'")


@pytest.mark.integration
def test_orphan_vectors_pruned_while_shared_active_content_is_retained(store):
    _, _, _, first = publish(store, repo_id="first", app="shared-app")
    _, _, _, second = publish(store, repo_id="second", app="shared-app")
    assert {chunk.content_hash for chunk in first.chunks} == {chunk.content_hash for chunk in second.chunks}
    with store._connect() as connection:
        for chunk in first.chunks:
            connection.execute(
                "INSERT INTO embedding_cache VALUES(%s,%s,%s,%s::vector)",
                ("fixture", chunk.content_hash, 2, "[1,0]"),
            )
        connection.execute(
            "INSERT INTO embedding_cache VALUES(%s,%s,%s,%s::vector)",
            ("fixture", "already-orphaned", 2, "[0,1]"),
        )
    publish(store, repo_id="first", app="new-first", commit="b" * 40)
    hashes = {row["content_hash"] for row in store._query("SELECT content_hash FROM embedding_cache")}
    assert hashes == {chunk.content_hash for chunk in second.chunks}
    publish(store, repo_id="second", app="new-second", commit="c" * 40)
    hashes = {row["content_hash"] for row in store._query("SELECT content_hash FROM embedding_cache")}
    assert second.chunks[0].content_hash not in hashes
    assert second.chunks[1].content_hash in hashes  # The database resource remains in both current scans.


@pytest.mark.integration
def test_expected_snapshot_and_point_lookups_never_expose_legacy_rows(store):
    from types import SimpleNamespace

    repo, latest, file, extracted = publish(store)
    stale_id = "legacy-scan"
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO snapshots(id,repo_id,commit_sha,fetched_at) VALUES(%s,%s,%s,now())",
            (stale_id, repo.id, "e" * 40),
        )
        connection.execute(
            "INSERT INTO files SELECT id||'-stale',repo_id,%s,path,payload FROM files WHERE id=%s",
            (stale_id, file.id),
        )
        connection.execute(
            "INSERT INTO resources SELECT id||'-stale',repo_id,%s,file_id||'-stale',kind,app,"
            "services,payload "
            "FROM resources WHERE id=%s",
            (stale_id, extracted.resources[0].id),
        )
        connection.execute(
            "INSERT INTO chunks SELECT id||'-stale',repo_id,%s,file_id||'-stale',content_hash,payload "
            "FROM chunks WHERE id=%s",
            (stale_id, extracted.chunks[0].id),
        )
        connection.execute(
            "INSERT INTO embedding_cache VALUES(%s,%s,%s,%s::vector)",
            ("fixture", extracted.chunks[0].content_hash, 2, "[1,0]"),
        )
        connection.execute(
            "UPDATE snapshots SET semantic_providers=ARRAY['fixture'] WHERE id=%s", (stale_id,)
        )
    assert store.resources(snapshot_id=stale_id)["items"] == []
    assert store.resources(snapshot_id=latest.id)["items"]
    assert store.resource(extracted.resources[0].id + "-stale")["error"] == "resource_not_found"
    assert [row["id"] for row in store.snapshots(repo.id)["items"]] == [latest.id]
    provider = SimpleNamespace(cache_key="fixture", dimensions=2, model="fixture")
    assert (
        store.similar_chunks(extracted.chunks[0].id + "-stale", provider)["error"]
        == "chunk_embedding_unavailable"
    )
    assert store._semantic([1, 0], provider, snapshot_id=stale_id)["available"] is False
    assert store._semantic([1, 0], provider, snapshot_id=stale_id)["selected_snapshots"] == 0


@pytest.mark.integration
def test_repository_metadata_exposes_latest_readiness_and_coverage(store):
    repo, snap, file, extracted = publish(store)
    extracted.skipped = [{"path": f"skipped-{number}.yaml", "reason": "too_large"} for number in range(30)]
    store.publish(repo, snap, [file], extracted)
    store._query(
        "UPDATE snapshots SET semantic_providers=ARRAY['configured-provider'] WHERE id=%s RETURNING id",
        (snap.id,),
    )
    result = store.repositories()["items"][0]
    assert result["current_snapshot"] == snap.id
    assert result["commit"] == snap.commit
    assert result["status"] == "indexed"
    assert result["semantic_providers"] == ["configured-provider"]
    assert result["skipped_count"] == 30
    assert result["skipped_truncated"]
    assert len(result["skipped"]) == 20
    assert result["extraction_hash"]


@pytest.mark.integration
def test_repository_contexts_share_one_view_during_refresh(store, monkeypatch):
    _, initial, _, _ = publish(store)
    connect = store._connect
    refreshed = False

    class ConnectionView:
        def __init__(self):
            self.connection = connect()

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, statement, params=()):
            nonlocal refreshed
            cursor = self.connection.execute(statement, params)
            if statement.startswith("SELECT p.*") and not refreshed:
                refreshed = True
                publish(store, app="replacement", commit="b" * 40)
            return cursor

    monkeypatch.setattr(store, "_connect", ConnectionView)
    result = store.repositories(services=["cnpg"])["items"][0]
    assert result["current_snapshot"] == initial.id
    assert result["contexts"]
    assert result["contexts"][0]["context"] == "kubernetes/apollo"
    assert "cloudnative-pg" in result["contexts"][0]["services"]
    assert store.current_snapshot("repo")["commit"] == "b" * 40


@pytest.mark.integration
def test_semantic_scan_change_returns_unavailable_not_stale_readiness(store, monkeypatch):
    from types import SimpleNamespace

    _, initial, _, _ = publish(store)
    provider = SimpleNamespace(cache_key="numeric-fixture", dimensions=2, model="numeric-fixture")
    store._query(
        "UPDATE snapshots SET semantic_providers=ARRAY[%s] WHERE id=%s RETURNING id",
        (provider.cache_key, initial.id),
    )
    query = store._query
    refreshed = False

    def refresh_after_initial_readiness(statement, params=()):
        nonlocal refreshed
        rows = query(statement, params)
        if statement.startswith("SELECT count(*) AS selected") and not refreshed:
            refreshed = True
            publish(store, app="replacement", commit="c" * 40)
        return rows

    monkeypatch.setattr(store, "_query", refresh_after_initial_readiness)
    result = store._semantic([1, 0], provider)
    assert result["available"] is False
    assert result["reason"] == "scan_changed"
    assert result["items"] == []
    assert result["ready_snapshots"] == 0
