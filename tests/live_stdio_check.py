"""Live MCP protocol check (not part of the pytest suite): spawns the server
over stdio with the installed mcp SDK client, lists tools, and exercises a
remember -> recall -> update -> stats round trip against a throwaway store."""

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    root = tempfile.mkdtemp(prefix="mnemo-live-")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mnemosyne.server"],
        env={**os.environ, "MNEMOSYNE_HOME": root, "MNEMOSYNE_PROVIDER": "dev"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names)
            assert names == sorted([
                "memory_remember", "memory_recall", "memory_update",
                "memory_reflect", "memory_forget", "memory_stats",
                "memory_reindex",
            ]), names

            upd = next(t for t in tools.tools if t.name == "memory_update")
            ann = getattr(upd, "annotations", None)
            print("update annotations:", ann)

            r = await session.call_tool("memory_remember", {
                "params": {"text": "Alice works at Initech.", "type": "world_fact",
                           "entities": ["Alice"]}})
            payload = json.loads(r.content[0].text)
            assert payload["status"] == "ok", payload
            mem_id = payload["id"]
            print("remembered:", mem_id)

            r = await session.call_tool("memory_update", {
                "params": {"id": mem_id, "text": "Alice works at Globex."}})
            payload = json.loads(r.content[0].text)
            assert payload["status"] == "ok", payload
            print("updated:", payload["text"])

            r = await session.call_tool("memory_recall", {
                "params": {"query": "where does Alice work?", "entities": ["Alice"]}})
            out = r.content[0].text
            assert "Globex" in out and "Initech" not in out, out
            print("recall ok (Globex found, Initech gone)")

            r = await session.call_tool("memory_stats", {})
            stats = json.loads(r.content[0].text)
            assert stats["total"] == 1, stats
            print("stats ok:", {k: stats[k] for k in ("total", "superseded", "never_recalled")})

    print("LIVE STDIO CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
