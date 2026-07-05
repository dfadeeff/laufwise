"""Drive rh serve over real stdio like an agent would: list tools, begin_step,
create_event, complete_step. Three processes: this client <-> rh serve <-> calendar MCP,
with laufwise verifying against the HTTP API."""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SCRATCH = sys.path[0]


async def main(lie: bool):
    wrap = f"python {SCRATCH}/calendar_mcp.py http://127.0.0.1:8975"
    if lie:
        wrap += " --lie"
    params = StdioServerParameters(
        command="rh",
        args=[
            "serve", f"{SCRATCH}/session_runbook.yaml",
            "--case", f"{SCRATCH}/session_case.json",
            "--wrap", wrap,
            "--output-dir", f"{SCRATCH}/runs",
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            names = sorted(t.name for t in (await session.list_tools()).tools)
            print(f"tools before begin_step: {names}")

            begun = json.loads((await session.call_tool("begin_step", {})).content[0].text)
            print(f"begin_step -> {begun['status']}, allowed={begun.get('allowed_tools')}")

            names = sorted(t.name for t in (await session.list_tools()).tools)
            print(f"tools while active:      {names}")

            claim = (await session.call_tool(
                "create_event", {"title": "Interview c-1", "start": "2026-07-08T14:00:00"}
            )).content[0].text
            print(f"agent's claim:           {claim}")

            done = json.loads((await session.call_tool("complete_step", {})).content[0].text)
            print(f"complete_step ->         {json.dumps(done)}")


if __name__ == "__main__":
    lie = "--lie" in sys.argv
    print(f"=== {'LYING downstream (write never lands)' if lie else 'HONEST downstream'} ===")
    asyncio.run(main(lie))
