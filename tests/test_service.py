import asyncio
import json
import subprocess
import sys
import threading

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from k8s_explorer.config import Settings
from k8s_explorer.http import create_app
from k8s_explorer.service import Explorer, result_budget


@pytest.fixture
def explorer(tmp_path):
    return Explorer(Settings(corpus_dir=tmp_path / "corpus"))


async def test_tool_schemas_and_disabled_semantic(explorer):
    tools = await explorer.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert len(names) == 13
    assert {"source_read_file", "structured_resources", "graph_neighbors", "semantic_search"} <= names
    assert not {"source_diff", "list_snapshots"} & names
    for tool in tools:
        assert "snapshot_id" not in tool.inputSchema.get("properties", {})
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.outputSchema
    result = explorer.call("semantic_search", {"query": "Postgres backup patterns"})
    assert result == {"available": False, "reason": "No embedding provider configured", "items": []}
    with pytest.raises(ToolError, match="Limit"):
        explorer.call("structured_resources", {"limit": 1000})
    with pytest.raises(ValidationError):
        explorer.call("structured_repositories", {"sort": "arbitrary sql"})


def test_queries_keep_native_filters_and_sanitize_errors(explorer, monkeypatch):
    def query(**kwargs):
        return {"filters": kwargs}

    monkeypatch.setattr(explorer.store, "resources", query)
    result = explorer.call("structured_resources", {"context": "kubernetes/apollo", "service": "cnpg"})
    assert result["filters"]["context"] == "kubernetes/apollo"
    assert result["filters"]["service"] == "cnpg"

    def failure(**kwargs):
        raise RuntimeError("postgres://user:do-not-expose@host/database")

    monkeypatch.setattr(explorer.store, "resources", failure)
    with pytest.raises(ToolError) as caught:
        explorer.call("structured_resources", {})
    assert "do-not-expose" not in str(caught.value)


async def test_http_auth_host_and_probe_behavior(tmp_path, monkeypatch):
    explorer = Explorer(Settings(corpus_dir=tmp_path, api_token="test-token"))
    app = create_app(explorer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/health/live", headers={"Host": "pod-ip"})).status_code == 200
        assert (await client.post("/mcp")).status_code == 401
        response = await client.post("/mcp", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        response = await client.post(
            "/mcp", headers={"Authorization": "Bearer test-token", "Host": "untrusted.example"}
        )
        assert response.status_code == 421

        def unavailable():
            raise RuntimeError("private database error")

        monkeypatch.setattr(explorer.store, "health", unavailable)
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert "private" not in response.text


def test_embedding_config_requires_whole_provider():
    with pytest.raises(ValidationError):
        Settings(embedding_url="http://localhost:8080/embeddings")
    settings = Settings(
        embedding_url="http://localhost:8080/embeddings",
        embedding_model="chosen-model",
        embedding_dimensions=384,
    )
    assert settings.embedding_model == "chosen-model"


def test_provider_settings_environment_metadata_and_output_dimensions(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPLORER_EMBEDDING_PROTOCOL", "tei")
    monkeypatch.setenv("EXPLORER_EMBEDDING_QUERY_PROMPT_NAME", "query")
    monkeypatch.setenv("EXPLORER_EMBEDDING_MODEL_REVISION", "pinned-model-sha")
    monkeypatch.setenv("EXPLORER_EMBEDDING_REQUESTED_DIMENSIONS", "384")
    monkeypatch.setenv("EXPLORER_EMBEDDING_REQUEST_TIMEOUT_SECONDS", "300")
    settings = Settings(
        corpus_dir=tmp_path,
        embedding_url="http://localhost:8080/embed",
        embedding_model="Qwen/example",
        embedding_dimensions=384,
        embedding_api_key="private-test-key",
    )
    configured = Explorer(settings)
    result = configured.call("describe_indexes", {})["semantic_provider"]
    assert result["protocol"] == "tei" and result["query_prompt_name"] == "query"
    assert result["model_revision"] == "pinned-model-sha" and result["requested_dimensions"] == 384
    assert result["request_timeout_seconds"] == 300
    assert "private-test-key" not in json.dumps(result)
    with pytest.raises(ValidationError, match="match"):
        Settings(embedding_url="http://localhost/embed", embedding_model="m", embedding_dimensions=1024)


@pytest.mark.parametrize("timeout", [0, 1801, float("nan"), float("inf")])
def test_embedding_timeout_settings_are_bounded(timeout):
    with pytest.raises(ValidationError):
        Settings(embedding_request_timeout_seconds=timeout)


def test_result_byte_budget_preserves_pagination():
    response = result_budget({"items": [{"text": "x" * 1000}] * 10}, offset=20, max_bytes=3000)
    assert 0 < len(response["items"]) < 10
    assert response["next_offset"] == 20 + len(response["items"])
    assert response["byte_budget_truncated"] is True
    assert len(json.dumps(response).encode()) <= 3000
    response = result_budget({"items": [{"text": "x" * 1000}] * 10}, max_bytes=3000, paginated=False)
    assert "next_offset" not in response
    with pytest.raises(ValueError, match="metadata"):
        result_budget({"items": [], "large_metadata": "x" * 3000}, max_bytes=3000)
    response = result_budget(
        {"items": [{"text": "x" * 1000}] * 10, "incomplete": False, "incomplete_reasons": []},
        max_bytes=3000,
        paginated=False,
    )
    assert response["incomplete"] is True
    assert "result_byte_budget" in response["incomplete_reasons"]


async def test_blocking_queries_leave_http_probes_responsive(explorer, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocking_query(**kwargs):
        entered.set()
        release.wait(timeout=5)
        return {"items": []}

    monkeypatch.setattr(explorer.store, "resources", blocking_query)
    tool = explorer.mcp._tool_manager.get_tool("structured_resources")
    pending = asyncio.create_task(tool.fn())
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        assert not pending.done()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(explorer)), base_url="http://testserver"
        ) as client:
            async with asyncio.timeout(1):
                assert (await client.get("/health/live")).status_code == 200
    finally:
        release.set()
        assert await pending == {"items": []}


def test_cli_tools_is_json_without_database(tmp_path):
    import os

    env = os.environ | {"EXPLORER_CORPUS_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "k8s_explorer.cli", "tools"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert len(json.loads(result.stdout)) == 13


def test_source_tools_resolve_current_scan_and_reject_stale_guards(explorer, monkeypatch):
    from k8s_explorer.models import Snapshot, stable_id

    repo_id = "a" * 32
    first = Snapshot(id=stable_id(repo_id, "1" * 40), repo_id=repo_id, commit="1" * 40)
    current = first
    monkeypatch.setattr(
        explorer.store, "current_snapshot", lambda _: {"id": current.id, "commit": current.commit}
    )
    monkeypatch.setattr(explorer.corpus, "snapshot", lambda _: current)
    monkeypatch.setattr(explorer.corpus, "read", lambda snapshot, *args: {"commit": snapshot.commit})
    assert (
        explorer.call("source_read_file", {"repo_id": repo_id, "path": "app.yaml"})["commit"] == first.commit
    )
    current = Snapshot(id=stable_id(repo_id, "2" * 40), repo_id=repo_id, commit="2" * 40)
    with pytest.raises(ToolError, match="scan changed"):
        explorer.call(
            "source_read_file", {"repo_id": repo_id, "path": "app.yaml", "expected_snapshot_id": first.id}
        )
    assert (
        explorer.call("source_read_file", {"repo_id": repo_id, "path": "app.yaml"})["commit"]
        == current.commit
    )
    with pytest.raises(ValidationError):
        explorer.call("structured_resources", {"snapshot_id": first.id})
    with pytest.raises(ValueError, match="Unknown tool"):
        explorer.call("source_diff", {})


def test_refresh_during_source_read_rejects_old_evidence(explorer, monkeypatch):
    from k8s_explorer.models import Snapshot, stable_id

    repo_id = "b" * 32
    old = Snapshot(id=stable_id(repo_id, "1" * 40), repo_id=repo_id, commit="1" * 40)
    newer = Snapshot(id=stable_id(repo_id, "2" * 40), repo_id=repo_id, commit="2" * 40)
    current = old
    monkeypatch.setattr(
        explorer.store, "current_snapshot", lambda _: {"id": current.id, "commit": current.commit}
    )
    monkeypatch.setattr(explorer.corpus, "snapshot", lambda _: old)

    def refresh_and_read(snapshot, *args):
        nonlocal current
        current = newer
        return {"text": "old source", "commit": snapshot.commit}

    monkeypatch.setattr(explorer.corpus, "read", refresh_and_read)
    with pytest.raises(ToolError, match="scan changed"):
        explorer.call("source_read_file", {"repo_id": repo_id, "path": "app.yaml"})
