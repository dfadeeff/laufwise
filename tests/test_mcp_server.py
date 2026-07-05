"""The MCP step session + scoped proxy (ARCHITECTURE §1.6), tested over real in-memory MCP
sessions: a FastMCP downstream "calendar" whose tools mutate a shared dict standing in for
the system of record, and a client driving the laufwise session the way an agent would.

The contract under test: tools are invisible/refused outside an active step; begin_step
gates against real state; complete_step believes only the re-queried state, never the
downstream tool's claim."""

import asyncio
import json

import pytest

mcp = pytest.importorskip("mcp")

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from laufwise.adapters.base import StubAdapter  # noqa: E402
from laufwise.approval.base import AutoApprovalGate  # noqa: E402
from laufwise.contract.evaluator import BuiltinEvaluator  # noqa: E402
from laufwise.engine.local import LocalEngine  # noqa: E402
from laufwise.mcp.server import (  # noqa: E402
    BEGIN_STEP,
    COMPLETE_STEP,
    RunbookMcpServer,
)
from laufwise.spec.models import (  # noqa: E402
    CheckSpec,
    RunbookSpec,
    StateBinding,
    StepSpec,
)
from laufwise.state.memory import MemoryStateProvider  # noqa: E402
from laufwise.trace.jsonl import JsonlTraceSink  # noqa: E402


def _spec() -> RunbookSpec:
    return RunbookSpec(
        runbook="booking",
        state={"event": StateBinding(provider="memory"), "slot": StateBinding(provider="memory")},
        steps=[
            StepSpec(
                id="book_slot",
                description="Book the interview slot",
                preconditions=[CheckSpec(expr="slot.exists == true")],
                tools=["create_event"],
                postconditions=[CheckSpec(expr="event.exists == true")],
            ),
            StepSpec(id="confirm", description="Confirmation step", tools=[]),
        ],
    )


def _calendar(record: dict, honest: bool) -> FastMCP:
    server = FastMCP("calendar")

    @server.tool()
    def create_event(title: str) -> str:
        if honest:
            record["event"] = {"title": title}
        return f"created {title!r}"  # the claim — identical whether honest or lying

    @server.tool()
    def delete_event(title: str) -> str:
        record.pop("event", None)
        return "deleted"

    return server


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


async def _drive(record: dict, honest: bool, scenario):
    """Wire downstream calendar + laufwise session over in-memory MCP, run `scenario`."""
    provider = MemoryStateProvider(record)
    engine = LocalEngine(
        provider=provider,
        evaluator=BuiltinEvaluator(),
        trace=JsonlTraceSink(record.pop("_trace_path")),
        approval=AutoApprovalGate(),
        adapter=StubAdapter(),
    )
    calendar = _calendar(record, honest)
    async with create_connected_server_and_client_session(calendar._mcp_server) as downstream:
        listed = await downstream.list_tools()
        tools = {t.name: (downstream, t) for t in listed.tools}
        runbook = RunbookMcpServer(_spec(), engine, tools)
        async with create_connected_server_and_client_session(runbook.server) as client:
            await scenario(client, runbook)
    engine.trace.close()


def test_tools_scoped_to_active_step(tmp_path):
    record = {"slot": {"free": True}, "event": None, "_trace_path": tmp_path / "ep.jsonl"}

    async def scenario(client, runbook):
        # before begin_step: only control tools visible, proxy refuses calls
        names = {t.name for t in (await client.list_tools()).tools}
        assert "create_event" not in names and BEGIN_STEP in names
        refused = _payload(await client.call_tool("create_event", {"title": "x"}))
        assert "no active step" in refused["refused"]

        begun = _payload(await client.call_tool(BEGIN_STEP, {}))
        assert begun["status"] == "step_active" and begun["allowed_tools"] == ["create_event"]

        # active: create_event visible, delete_event (not allowlisted) still refused
        names = {t.name for t in (await client.list_tools()).tools}
        assert "create_event" in names and "delete_event" not in names
        refused = _payload(await client.call_tool("delete_event", {"title": "x"}))
        assert "not in the allowlist" in refused["refused"]

    asyncio.run(_drive(record, honest=True, scenario=scenario))


def test_honest_tool_verifies_and_advances(tmp_path):
    record = {"slot": {"free": True}, "event": None, "_trace_path": tmp_path / "ep.jsonl"}

    async def scenario(client, runbook):
        await client.call_tool(BEGIN_STEP, {})
        await client.call_tool("create_event", {"title": "Interview c-1"})
        done = _payload(await client.call_tool(COMPLETE_STEP, {}))
        assert done["status"] == "ok" and done["next_step"] == "confirm"
        # step 2 has no tools/checks: begin + complete finishes the runbook
        await client.call_tool(BEGIN_STEP, {})
        final = _payload(await client.call_tool(COMPLETE_STEP, {}))
        assert final["status"] == "ok" and final.get("runbook") == "complete"

    asyncio.run(_drive(record, honest=True, scenario=scenario))
    assert record["event"] == {"title": "Interview c-1"}


def test_lying_tool_is_rejected_and_session_halts(tmp_path):
    record = {"slot": {"free": True}, "event": None, "_trace_path": tmp_path / "ep.jsonl"}

    async def scenario(client, runbook):
        await client.call_tool(BEGIN_STEP, {})
        claim = (await client.call_tool("create_event", {"title": "Interview c-1"})).content[0].text
        assert "created" in claim  # downstream claims success...
        done = _payload(await client.call_tool(COMPLETE_STEP, {}))
        assert done["status"] == "reject"  # ...but the state re-query says otherwise
        # halted: step N+1 cannot begin
        refused = _payload(await client.call_tool(BEGIN_STEP, {}))
        assert "halted" in refused["refused"]

    asyncio.run(_drive(record, honest=False, scenario=scenario))


def test_failing_precondition_blocks_begin_step(tmp_path):
    record = {"slot": None, "event": None, "_trace_path": tmp_path / "ep.jsonl"}  # no free slot

    async def scenario(client, runbook):
        blocked = _payload(await client.call_tool(BEGIN_STEP, {}))
        assert blocked["status"] == "block" and blocked["check"] == "slot.exists == true"
        # nothing became callable
        refused = _payload(await client.call_tool("create_event", {"title": "x"}))
        assert "no active step" in refused["refused"]

    asyncio.run(_drive(record, honest=True, scenario=scenario))