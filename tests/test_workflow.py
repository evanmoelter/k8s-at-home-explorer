import os
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest
import uvicorn
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp.exceptions import ToolError
from psycopg import sql

from k8s_explorer.config import Settings
from k8s_explorer.http import create_app
from k8s_explorer.runtime import sync_catalogue
from k8s_explorer.service import Explorer

pytestmark = pytest.mark.integration


@pytest.fixture
def workflow(tmp_path):
    url = os.environ.get("EXPLORER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set EXPLORER_TEST_DATABASE_URL for the disposable PostgreSQL workflow")
    repo_dir = tmp_path / "upstream"
    repo_dir.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Explorer test")
    app_dir = repo_dir / "kubernetes" / "apollo" / "apps" / "demo"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text("""apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: demo
  namespace: apps
spec:
  dependsOn:
    - name: database
  values:
    image:
      repository: ghcr.io/example/demo
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: database
  namespace: apps
spec:
  chart:
    spec:
      chart: cloudnative-pg
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: cilium
  namespace: kube-system
""")
    git("add", ".")
    git("commit", "-m", "fixture")
    catalogue = tmp_path / "repositories.yaml"
    catalogue.write_text(yaml.safe_dump({"repositories": [{"url": str(repo_dir), "branch": "main"}]}))
    explorer = Explorer(
        Settings(
            corpus_dir=tmp_path / "corpus",
            database_url=url,
            repositories_file=catalogue,
            allow_local_repos=True,
            api_token="workflow-token",
        )
    )
    schema = "workflow_" + uuid.uuid4().hex
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connect = explorer.store._connect

    def scoped_connection():
        conn = connect()
        conn.execute(sql.SQL("SET search_path TO {},public").format(sql.Identifier(schema)))
        return conn

    explorer.store._connect = scoped_connection
    yield explorer, catalogue, app_dir, git
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_sync_and_cross_family_workflow(workflow):
    explorer, catalogue, app_dir, git = workflow
    first = sync_catalogue(explorer, catalogue)
    assert first["failed"] == 0, first
    snapshot_id = first["items"][0]["snapshot_id"]
    repo_id = first["items"][0]["repo_id"]
    repos = explorer.call("structured_repositories", {"services": ["cilium", "cnpg"], "app": "demo"})
    assert len(repos["items"]) == 1
    demo = explorer.call("structured_resources", {"app": "demo"})["items"][0]
    edges = explorer.call("graph_neighbors", {"node_id": demo["id"]})["items"]
    database = explorer.call("structured_read_resource", {"resource_id": edges[0]["target_id"]})
    assert "cloudnative-pg" in database["services"]
    source = explorer.call(
        "source_read_file", {"repo_id": repo_id, "expected_snapshot_id": snapshot_id, "path": demo["path"]}
    )
    assert "repository: ghcr.io/example/demo" in source["text"]
    assert source["commit"] == demo["commit"]
    (app_dir / "app.yaml").write_text(
        (app_dir / "app.yaml").read_text().replace("name: demo", "name: changed")
    )
    git("add", ".")
    git("commit", "-m", "update")
    second = sync_catalogue(explorer, catalogue)
    assert second["failed"] == 0
    assert explorer.call("structured_resources", {"app": "demo"})["items"] == []
    with pytest.raises(ToolError, match="scan changed"):
        explorer.call(
            "source_read_file",
            {"repo_id": repo_id, "expected_snapshot_id": snapshot_id, "path": demo["path"]},
        )
    assert explorer.call("structured_read_resource", {"resource_id": demo["id"]}) == {
        "error": "resource_not_found"
    }
    assert len(explorer.store.snapshots()["items"]) == 1
    assert not (explorer.corpus.root / "snapshots" / f"{snapshot_id}.json").exists()
    assert (
        "name: changed"
        in explorer.call("source_read_file", {"repo_id": repo_id, "path": demo["path"]})["text"]
    )


def test_ingestion_source_budget_is_reported(workflow):
    explorer, catalogue, _, _ = workflow
    explorer.settings.max_repo_source_bytes = 10
    result = sync_catalogue(explorer, catalogue)
    assert result["failed"] == 0
    assert result["items"][0]["source_bytes"] == 0
    assert explorer.store.resources()["items"] == []
    snapshot = explorer.store.snapshots()["items"][0]
    assert any(s["reason"] == "repository source byte budget exceeded" for s in snapshot["skipped"])


def test_overlapping_syncs_publish_in_fetch_order(workflow, monkeypatch):
    import k8s_explorer.runtime as runtime

    explorer, catalogue, app_dir, git = workflow
    entered, release = threading.Event(), threading.Event()
    extract = runtime.extract
    first_extraction = True

    def paused_extract(*args):
        nonlocal first_extraction
        if first_extraction:
            first_extraction = False
            entered.set()
            assert release.wait(timeout=10)
        return extract(*args)

    monkeypatch.setattr(runtime, "extract", paused_extract)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(sync_catalogue, explorer, catalogue)
        try:
            assert entered.wait(timeout=10)
            source = app_dir / "app.yaml"
            source.write_text(source.read_text().replace("name: demo", "name: newer"))
            git("add", ".")
            git("commit", "-m", "newer upstream")
            newer_commit = git("rev-parse", "HEAD")
            second = workers.submit(sync_catalogue, explorer, catalogue)
            time.sleep(0.1)
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=10)["failed"] == 0
        assert second.result(timeout=10)["failed"] == 0
    assert explorer.store.repositories()["items"][0]["commit"] == newer_commit
    assert explorer.store.resources(app="newer")["items"]


async def test_real_http_mcp_transport(workflow):
    explorer, catalogue, _, _ = workflow
    assert sync_catalogue(explorer, catalogue)["failed"] == 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(explorer), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        async with (
            httpx.AsyncClient(headers={"Authorization": "Bearer workflow-token"}) as client,
            streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (read, write, _),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 13
                result = await session.call_tool("structured_repositories", {"services": ["cilium", "cnpg"]})
                assert not result.isError
                assert result.structuredContent["items"]
                repository = result.structuredContent["items"][0]
                result = await session.call_tool(
                    "source_list_files",
                    {"repo_id": repository["id"], "expected_snapshot_id": repository["current_snapshot"]},
                )
                assert result.structuredContent["items"][0]["path"].endswith("app.yaml")
                result = await session.call_tool("semantic_search", {"query": "database backup"})
                assert result.structuredContent["available"] is False
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_failed_publication_preserves_last_successful_scan(workflow, monkeypatch):
    explorer, catalogue, app_dir, git = workflow
    first = sync_catalogue(explorer, catalogue)["items"][0]
    path = app_dir / "app.yaml"
    path.write_text(path.read_text().replace("name: demo", "name: candidate"))
    git("add", ".")
    git("commit", "-m", "unpublished candidate")
    publish = explorer.store.publish

    def unavailable(*args):
        raise ValueError("Publication unavailable")

    monkeypatch.setattr(explorer.store, "publish", unavailable)
    assert sync_catalogue(explorer, catalogue)["failed"] == 1
    assert explorer.store.current_snapshot(first["repo_id"])["id"] == first["snapshot_id"]
    source = explorer.call(
        "source_read_file", {"repo_id": first["repo_id"], "path": "kubernetes/apollo/apps/demo/app.yaml"}
    )
    assert source["commit"] == first["commit"] and "name: demo" in source["text"]
    monkeypatch.setattr(explorer.store, "publish", publish)
    result = sync_catalogue(explorer, catalogue)
    assert result["failed"] == 0
    assert explorer.store.resources(app="candidate")["items"]
    assert not (explorer.corpus.root / "snapshots" / f"{first['snapshot_id']}.json").exists()


def test_cleanup_failure_preserves_latest_and_recovers_on_next_scan(workflow, monkeypatch):
    explorer, catalogue, app_dir, git = workflow
    first = sync_catalogue(explorer, catalogue)["items"][0]
    path = app_dir / "app.yaml"
    path.write_text(path.read_text().replace("name: demo", "name: next"))
    git("add", ".")
    git("commit", "-m", "next scan")
    prune = explorer.corpus.prune

    def interrupted(*args):
        raise ValueError("Cleanup interrupted")

    monkeypatch.setattr(explorer.corpus, "prune", interrupted)
    second = sync_catalogue(explorer, catalogue)
    assert second["failed"] == 0
    assert second["items"][0]["source_cleanup"] == {"error_type": "ValueError"}
    assert explorer.store.current_snapshot(first["repo_id"])["commit"] == git("rev-parse", "HEAD")
    assert explorer.call("structured_resources", {"app": "next"})["items"]
    with pytest.raises(ToolError, match="scan changed"):
        explorer.call(
            "source_list_files", {"repo_id": first["repo_id"], "expected_snapshot_id": first["snapshot_id"]}
        )
    monkeypatch.setattr(explorer.corpus, "prune", prune)
    assert sync_catalogue(explorer, catalogue)["failed"] == 0
    assert not (explorer.corpus.root / "snapshots" / f"{first['snapshot_id']}.json").exists()


def test_deleted_files_and_resources_are_absent_from_latest_tools(workflow):
    explorer, catalogue, app_dir, git = workflow
    first = sync_catalogue(explorer, catalogue)["items"][0]
    resource = explorer.call("structured_resources", {"app": "demo"})["items"][0]
    (app_dir / "app.yaml").unlink()
    git("add", "-A")
    git("commit", "-m", "remove app")
    assert sync_catalogue(explorer, catalogue)["failed"] == 0
    assert explorer.call("structured_resources", {"app": "demo"})["items"] == []
    assert explorer.call("structured_read_resource", {"resource_id": resource["id"]}) == {
        "error": "resource_not_found"
    }
    assert explorer.call("graph_neighbors", {"node_id": resource["id"]})["items"] == []
    assert explorer.call("source_list_files", {"repo_id": first["repo_id"]})["items"] == []
    with pytest.raises(ToolError):
        explorer.call("source_read_file", {"repo_id": first["repo_id"], "path": resource["path"]})
