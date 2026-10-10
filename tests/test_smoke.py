"""Serving protocol checks; fixtures contain source envelopes, never synthetic vectors."""

import hashlib
from types import SimpleNamespace

import pytest

from k8s_explorer.smoke import ServingCheckError, check_serving, check_session


@pytest.fixture
def session():
    text = "apiVersion: v1\nkind: ConfigMap"
    evidence = {
        "repo_id": "repo",
        "snapshot_id": "snapshot",
        "commit": "a" * 40,
        "path": "app.yaml",
        "start_line": 1,
        "end_line": 2,
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
    }
    responses = {
        "describe_indexes": {
            "semantic_provider": {
                "model": "voyage-4",
                "dimensions": 1024,
                "protocol": "voyage",
                "provider_id": "p",
            }
        },
        "semantic_search": {
            "available": True,
            "selected_snapshots": 20,
            "ready_snapshots": 20,
            "provider": "p",
            "items": [evidence],
        },
        "source_read_file": {
            "commit": "a" * 40,
            "snapshot_id": "snapshot",
            "text": text + "\n",
            "truncated": False,
        },
    }
    calls = []

    async def list_tools():
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name=name)
                for name in (
                    "describe_indexes",
                    "semantic_search",
                    "source_read_file",
                    "structured_repositories",
                )
            ]
        )

    async def call_tool(name, args):
        calls.append((name, args))
        return SimpleNamespace(isError=False, structuredContent=responses[name])

    return SimpleNamespace(list_tools=list_tools, call_tool=call_tool, responses=responses, calls=calls)


async def test_source_check_uses_snapshot_guard_and_accepts_normalized_line_endings(session):
    session.responses["source_read_file"]["text"] = "apiVersion: v1\r\nkind: ConfigMap\r\n"
    result = await check_session(session, "configuration", min_ready_repositories=20)
    assert result["status"] == "ok" and result["verified_results"] == 1
    assert session.calls[-1] == (
        "source_read_file",
        {
            "repo_id": "repo",
            "expected_snapshot_id": "snapshot",
            "path": "app.yaml",
            "start_line": 1,
            "end_line": 2,
        },
    )


@pytest.mark.parametrize(
    "tool,key,value",
    [
        ("describe_indexes", "semantic_provider", {}),
        ("semantic_search", "ready_snapshots", 19),
        ("semantic_search", "available", False),
        ("semantic_search", "items", []),
        ("semantic_search", "provider", "different"),
        ("source_read_file", "commit", "b" * 40),
        ("source_read_file", "snapshot_id", "different"),
        ("source_read_file", "truncated", True),
        ("source_read_file", "text", "changed text"),
    ],
)
async def test_incomplete_or_mismatched_evidence_fails(session, tool, key, value):
    session.responses[tool][key] = value
    with pytest.raises(ServingCheckError):
        await check_session(session, "configuration", min_ready_repositories=20)


async def test_minimum_repository_count_is_enforced(session):
    with pytest.raises(ServingCheckError):
        await check_session(session, "configuration", min_ready_repositories=21)


@pytest.mark.parametrize(
    "url,token",
    [
        ("https://user:private@example.invalid/mcp", "token"),
        ("https://example.invalid/mcp?token=private", "token"),
        ("https://example.invalid/mcp", ""),
        ("https://example.invalid/mcp", "private\nheader"),
        ("file:///tmp/mcp", "token"),
    ],
)
async def test_invalid_endpoint_and_token_fail_without_network_or_secret_leaks(url, token):
    with pytest.raises(ServingCheckError) as caught:
        await check_serving(url, token, "query")
    assert "private" not in str(caught.value)
