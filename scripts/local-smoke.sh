#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/local-common.sh"
require_local_cluster
local_kubectl -n "$LOCAL_NAMESPACE" exec -i deployment/k8s-explorer -c api -- python - <<'PY'
import asyncio
import json
import os
from pathlib import Path
from urllib.request import urlopen

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

assert os.getuid() == 568
for route in ("live", "ready"):
    with urlopen(f"http://127.0.0.1:8000/health/{route}") as response:
        assert response.status == 200
        print(f"health/{route}: {response.read().decode()}")
try:
    Path("/app/write-test").write_text("unexpected")
except OSError:
    print("Application filesystem is read-only.")
else:
    raise AssertionError("Application filesystem must be read-only")

async def main():
    async with streamable_http_client("http://127.0.0.1:8000/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert all(tool.outputSchema for tool in tools.tools)
            names = {tool.name for tool in tools.tools}
            assert len(names) == 13
            assert not {"source_diff", "list_snapshots"} & names
            for family in ("source_", "structured_", "graph_", "semantic_"):
                assert any(name.startswith(family) for name in names)
            print(f"MCP initialized; {len(names)} tools across all retrieval families.")
            result = await session.call_tool("describe_indexes", {})
            assert not result.isError
            output = result.structuredContent
            assert isinstance(output, dict) and output
            print(json.dumps(output, default=str))

asyncio.run(main())
PY
