"""Read-only serving checks; never writes to an installed database or corpus."""

import asyncio
import hashlib

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class ServingCheckError(ValueError):
    pass


async def check_session(
    session: ClientSession,
    query: str,
    *,
    expected_model: str = "voyage-4",
    expected_dimensions: int = 1024,
    expected_protocol: str = "voyage",
    min_ready_repositories: int = 1,
) -> dict:
    """Verify configured identity, complete selected indexes, and exact source evidence."""
    if not isinstance(query, str) or not query.strip() or len(query.encode()) > 8192:
        raise ServingCheckError("Supply a nonempty query of at most 8192 bytes")
    if not 1 <= min_ready_repositories <= 10000 or not 1 <= expected_dimensions <= 16000:
        raise ServingCheckError("Invalid readiness or dimension bound")

    async def call(name, arguments):
        result = await session.call_tool(name, arguments)
        if result.isError or not isinstance(result.structuredContent, dict):
            raise ServingCheckError("A serving check tool failed; inspect service logs")
        return result.structuredContent

    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    required = {"describe_indexes", "semantic_search", "source_read_file", "structured_repositories"}
    if not required <= names:
        raise ServingCheckError("Required retrieval tools are unavailable")
    description = await call("describe_indexes", {})
    provider = description.get("semantic_provider") or {}
    if (
        provider.get("model") != expected_model
        or provider.get("dimensions") != expected_dimensions
        or provider.get("protocol") != expected_protocol
    ):
        raise ServingCheckError("Serving provider does not match the expected model and dimensions")
    semantic = await call("semantic_search", {"query": query, "limit": 3})
    selected, ready = semantic.get("selected_snapshots", 0), semantic.get("ready_snapshots", 0)
    if (
        semantic.get("available") is not True
        or not semantic.get("items")
        or ready < min_ready_repositories
        or selected != ready
        or semantic.get("provider") != provider.get("provider_id")
    ):
        raise ServingCheckError(
            "Semantic indexes are unavailable or incomplete for the selected repositories"
        )
    for item in semantic["items"]:
        source = await call(
            "source_read_file",
            {
                "repo_id": item["repo_id"],
                "expected_snapshot_id": item["snapshot_id"],
                "path": item["path"],
                "start_line": item["start_line"],
                "end_line": item["end_line"],
            },
        )
        # Extraction normalizes line endings and omits the final line terminator.
        # Compare every source line, preserving its other whitespace.
        content = "\n".join(source["text"].splitlines())
        digest = hashlib.sha256(content.encode()).hexdigest()
        if (
            source.get("commit") != item["commit"]
            or source.get("snapshot_id") != item["snapshot_id"]
            or source.get("truncated")
            or content != item["content"]
            or digest != item["content_hash"]
        ):
            raise ServingCheckError("Retrieved evidence does not match its current source range")
    return {
        "status": "ok",
        "model": expected_model,
        "dimensions": expected_dimensions,
        "selected_repositories": selected,
        "ready_repositories": ready,
        "verified_results": len(semantic["items"]),
    }


async def check_serving(url: str, token: str, query: str, **kwargs) -> dict:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        raise ServingCheckError("Invalid MCP endpoint") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not token
        or "\r" in token
        or "\n" in token
    ):
        raise ServingCheckError("Supply an HTTP(S) endpoint without credentials and a separate bearer token")
    result, failure = None, None
    try:
        # Require authentication to reject an unconfigured or incorrectly routed endpoint.
        async with asyncio.timeout(120):
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                response = await client.post(str(parsed))
                if response.status_code != 401:
                    raise ServingCheckError("MCP endpoint did not reject an unauthenticated request")
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + token}, timeout=60, follow_redirects=False
            ) as client:
                async with streamable_http_client(str(parsed), http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        try:
                            result = await check_session(session, query, **kwargs)
                        except ServingCheckError as exc:
                            # Let MCP task groups close normally before raising our safe check error.
                            failure = str(exc)
    except ServingCheckError:
        raise
    except Exception:
        # SDK exception groups and HTTP exceptions may contain credentials or response bodies.
        raise ServingCheckError("MCP transport failed; check endpoint access and authentication") from None
    if failure is not None:
        raise ServingCheckError(failure)
    if result is None:
        raise ServingCheckError("Serving check returned no result")
    return result
