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


async def _drive(record: dict, honest: bool, scenario, spec: RunbookSpec | None = None):
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
        runbook = RunbookMcpServer(spec or _spec(), engine, tools)
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


def test_proxied_calls_land_in_the_receipt_not_just_refusals(tmp_path):
    # Tracing only refusals would leave the audit record able to show what the harness
    # stopped but not what it let through. "What the agent actually did" is the half a
    # receipt exists to prove.
    trace_path = tmp_path / "ep.jsonl"
    record = {"slot": {"free": True}, "event": None, "_trace_path": trace_path}

    async def scenario(client, runbook):
        await client.call_tool(BEGIN_STEP, {})
        await client.call_tool("create_event", {"title": "Interview c-1"})
        await client.call_tool("delete_event", {"title": "x"})  # not allowlisted -> refused
        await client.call_tool(COMPLETE_STEP, {})

        ruling = runbook.results[-1]
        assert [c["tool"] for c in ruling.tool_calls] == ["create_event"]
        assert ruling.state_hash_before and ruling.state_hash_after
        assert ruling.state_hash_before != ruling.state_hash_after  # the write landed

    asyncio.run(_drive(record, honest=True, scenario=scenario))

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    by_status = {e["status"] for e in events}
    assert "tool_call" in by_status  # the allowed call
    assert "refused" in by_status  # and the blocked one
    allowed = next(e for e in events if e["status"] == "tool_call")
    assert allowed["tool"] == "create_event"
    assert "Interview c-1" not in trace_path.read_text()  # args hashed, never verbatim


def test_reject_with_goto_routes_session_instead_of_halting(tmp_path):
    # The write step's postcondition fails (lying downstream tool), but on_fail=goto routes
    # the session to the escalation step instead of ending it — same rule as engine.run.
    spec = RunbookSpec(
        runbook="booking",
        state={"event": StateBinding(provider="memory"), "slot": StateBinding(provider="memory")},
        steps=[
            StepSpec(
                id="book_slot",
                preconditions=[CheckSpec(expr="slot.exists == true")],
                tools=["create_event"],
                postconditions=[CheckSpec(expr="event.exists == true")],
                on_fail="goto(escalate)",
            ),
            StepSpec(id="escalate", description="hand the case to a human", tools=[]),
        ],
    )
    record = {"slot": {"free": True}, "event": None, "_trace_path": tmp_path / "ep.jsonl"}

    async def scenario(client, runbook):
        await client.call_tool(BEGIN_STEP, {})
        await client.call_tool("create_event", {"title": "x"})  # claims success, writes nothing
        done = _payload(await client.call_tool(COMPLETE_STEP, {}))
        assert done["status"] == "reject"
        assert done["next_step"] == "escalate"
        # not halted: the routed step begins and the runbook can finish
        begun = _payload(await client.call_tool(BEGIN_STEP, {}))
        assert begun["status"] == "step_active" and begun["step"] == "escalate"
        final = _payload(await client.call_tool(COMPLETE_STEP, {}))
        assert final["status"] == "ok" and final.get("runbook") == "complete"

    asyncio.run(_drive(record, honest=False, scenario=scenario, spec=spec))


def test_failing_precondition_blocks_begin_step(tmp_path):
    record = {"slot": None, "event": None, "_trace_path": tmp_path / "ep.jsonl"}  # no free slot

    async def scenario(client, runbook):
        blocked = _payload(await client.call_tool(BEGIN_STEP, {}))
        assert blocked["status"] == "block" and blocked["check"] == "slot.exists == true"
        # nothing became callable
        refused = _payload(await client.call_tool("create_event", {"title": "x"}))
        assert "no active step" in refused["refused"]

    asyncio.run(_drive(record, honest=True, scenario=scenario))