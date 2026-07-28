"""RunbookMcpServer — the step session + scoped proxy (ARCHITECTURE §1.6).

The runbook session is a deterministic state machine driven by explicit agent requests:

    begin_step     -> engine.gate_step: preconditions vs real state + approval. BLOCK halts.
    <proxied tools> -> forwarded ONLY while a step is active and ONLY if allowlisted for it;
                       via tools/list_changed they are also the only tools *visible*.
    complete_step  -> engine.verify_step: postconditions re-queried against the system of
                      record (with verify retries). Step N+1 cannot begin until N verified.

The agent *requests* transitions; the engine *rules* on them — no step boundary is ever
inferred from traffic (CLAUDE.md invariant #1). Refused calls are traced: the violation is
part of the audit record, not just an error string.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server

from laufwise.engine.base import StepResult, StepStatus, tool_call_record
from laufwise.engine.local import LocalEngine
from laufwise.spec.models import RunbookSpec, StepSpec

BEGIN_STEP = "begin_step"
COMPLETE_STEP = "complete_step"
RUNBOOK_STATUS = "runbook_status"

_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def _text(payload: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


class RunbookMcpServer:
    """One MCP session = one runbook run. Downstream tools are provided as
    {tool_name: (client_session, tool)} — connected over stdio by `rh serve`, or injected
    in-memory by tests."""

    def __init__(
        self,
        spec: RunbookSpec,
        engine: LocalEngine,
        downstream: dict[str, tuple[Any, types.Tool]] | None = None,
    ) -> None:
        self.spec = spec
        self.engine = engine
        self.downstream = downstream or {}
        self.index = 0
        self._index_of = {step.id: i for i, step in enumerate(spec.steps)}
        self.visits: dict[str, int] = {}
        self.step_active = False
        # Receipt material for the step currently in flight, reset at every begin_step.
        self._step_tool_calls: list[dict] = []
        self._pre_hash: str | None = None
        self.halted: StepResult | None = None
        self.results: list[StepResult] = []
        self.server: Server = Server(f"laufwise:{spec.runbook}")
        self.server.list_tools()(self._list_tools)
        self.server.call_tool()(self._call_tool)

    # --- session state ------------------------------------------------------
    @property
    def current_step(self) -> StepSpec | None:
        if self.index < len(self.spec.steps):
            return self.spec.steps[self.index]
        return None

    @property
    def finished(self) -> bool:
        return self.halted is not None or self.current_step is None

    def _status(self) -> dict[str, Any]:
        step = self.current_step
        return {
            "runbook": self.spec.runbook,
            "steps_total": len(self.spec.steps),
            "steps_done": self.index,
            "current_step": step.id if step else None,
            "step_active": self.step_active,
            "halted": self.halted.status.value if self.halted else None,
            "halted_reason": self.halted.reason if self.halted else None,
        }

    # --- tool surface: control tools + the ACTIVE step's allowlist only ------
    async def _list_tools(self) -> list[types.Tool]:
        tools = [
            types.Tool(
                name=RUNBOOK_STATUS,
                description="Current runbook position: step, active/halted, progress.",
                inputSchema=_NO_ARGS,
            ),
            types.Tool(
                name=BEGIN_STEP,
                description=(
                    "Request the current step. The engine checks preconditions against the "
                    "real system of record and the approval gate; only then do the step's "
                    "allowlisted tools become available."
                ),
                inputSchema=_NO_ARGS,
            ),
            types.Tool(
                name=COMPLETE_STEP,
                description=(
                    "Declare the current step done. The engine re-queries the system of "
                    "record and verifies the step's postconditions — the claim alone is "
                    "never sufficient. On success the next step unlocks."
                ),
                inputSchema=_NO_ARGS,
            ),
        ]
        step = self.current_step
        if self.step_active and step is not None:
            for name in step.tools:
                entry = self.downstream.get(name)
                if entry is not None:
                    tools.append(entry[1])
        return tools

    async def _call_tool(
        self, name: str, arguments: dict | None
    ) -> list[types.TextContent] | tuple[list[types.TextContent], dict]:
        if name == RUNBOOK_STATUS:
            return _text(self._status())
        if name == BEGIN_STEP:
            return await self._begin_step()
        if name == COMPLETE_STEP:
            return await self._complete_step()
        return await self._proxy(name, arguments or {})

    # --- transitions: the agent requests, the engine rules -------------------
    async def _begin_step(self) -> list[types.TextContent]:
        if self.halted is not None:
            return _text({"refused": f"runbook halted: {self.halted.status.value}", **self._status()})
        step = self.current_step
        if step is None:
            return _text({"refused": "runbook already complete", **self._status()})
        if self.step_active:
            return _text({"refused": f"step {step.id!r} already active — call {COMPLETE_STEP}"})

        failure, pre_hash = await anyio.to_thread.run_sync(
            self.engine.gate_step, self.spec, step
        )
        if failure is not None:
            self._record(failure)
            self.halted = failure
            await self._notify_tools_changed()
            return _text({
                "status": failure.status.value,
                "step": step.id,
                "reason": failure.reason,
                "check": failure.expr,
                "blocked_tool": failure.blocked_tool,
            })

        self.step_active = True
        self._pre_hash = pre_hash
        self._step_tool_calls = []
        self.visits[step.id] = self.visits.get(step.id, 0) + 1
        await self._notify_tools_changed()
        return _text({
            "status": "step_active",
            "step": step.id,
            "description": step.description,
            "allowed_tools": [t for t in step.tools if t in self.downstream],
            "postconditions": [c.expr for c in step.postconditions],
            "note": f"do the work with the allowed tools, then call {COMPLETE_STEP}",
        })

    async def _complete_step(self) -> list[types.TextContent]:
        step = self.current_step
        if not self.step_active or step is None:
            return _text({"refused": f"no active step — call {BEGIN_STEP} first", **self._status()})

        result = await anyio.to_thread.run_sync(
            self.engine.verify_step, self.spec, step, self._pre_hash, self._step_tool_calls
        )
        self._record(result)
        self.step_active = False
        routed_to: str | None = None
        if result.status is StepStatus.OK:
            self.index += 1
        elif result.status is StepStatus.REJECT:
            # Same routing rule as engine.run: on_fail goto re-positions the session
            # (bounded by max_step_visits); halt/exhaustion ends it.
            routed_to, halt_reason = self.engine.route_reject(self.spec, step, self.visits)
            if routed_to is not None:
                self.index = self._index_of[routed_to]
            else:
                if halt_reason:
                    self.engine.trace.event(step_id=step.id, status="halt", reason=halt_reason)
                self.halted = result
        else:
            # STATE_UNAVAILABLE / CHECK_ERROR end the session: the outcome is unverified,
            # and on_fail routing applies to REJECT only.
            self.halted = result
        await self._notify_tools_changed()

        payload: dict[str, Any] = {"status": result.status.value, "step": step.id}
        if result.status is StepStatus.OK:
            nxt = self.current_step
            payload["next_step"] = nxt.id if nxt else None
            if nxt is None:
                payload["runbook"] = "complete"
        else:
            payload.update({"reason": result.reason, "check": result.expr})
            if routed_to is not None:
                payload["next_step"] = routed_to
                payload["note"] = f"on_fail routed to {routed_to!r} — call {BEGIN_STEP}"
        return _text(payload)

    # --- the scoped proxy -----------------------------------------------------
    async def _proxy(
        self, name: str, arguments: dict
    ) -> list[types.TextContent] | tuple[list[types.TextContent], dict]:
        step = self.current_step
        if not self.step_active or step is None:
            self._trace_refusal(name, "no_active_step")
            return _text({"refused": f"tool {name!r}: no active step — call {BEGIN_STEP} first"})
        if name not in step.tools or name not in self.downstream:
            self._trace_refusal(name, "tool_not_allowed", step)
            return _text({
                "refused": f"tool {name!r} is not in the allowlist of step {step.id!r}",
                "allowed_tools": [t for t in step.tools if t in self.downstream],
            })
        session, _tool = self.downstream[name]
        result = await session.call_tool(name, arguments)
        # An ALLOWED call is evidence too. Tracing only refusals would leave the audit
        # record able to show what the harness stopped but not what it let through — and
        # "what the agent actually did" is the half a receipt exists to prove.
        self._record_tool_call(step, name, arguments, ok=not getattr(result, "isError", False))
        # Full-fidelity passthrough: preserve structured output so the downstream tool's
        # declared outputSchema still validates on our side of the proxy.
        if result.structuredContent is not None:
            return list(result.content), result.structuredContent
        return list(result.content)

    # --- trace: every ruling is part of the audit record ----------------------
    def _record(self, result: StepResult) -> None:
        self.results.append(result)
        self.engine.trace.event(**result.trace_fields())

    def _record_tool_call(
        self, step: StepSpec, tool: str, arguments: dict, ok: bool
    ) -> None:
        """Add a proxied call to the step's receipt and emit it as its own trace event.

        Both, deliberately: the per-call event preserves ordering and timing, while the
        list attached to the step's ruling is what makes the ruling self-contained —
        before-state, calls, after-state in one record.
        """
        call = tool_call_record(tool, arguments, ok=ok)
        self._step_tool_calls.append(call)
        self.engine.trace.event(step_id=step.id, status="tool_call", **call)

    def _trace_refusal(self, tool: str, reason: str, step: StepSpec | None = None) -> None:
        self.engine.trace.event(
            step_id=step.id if step else None,
            status="refused",
            reason=reason,
            blocked_tool=tool,
        )

    async def _notify_tools_changed(self) -> None:
        # No live session context (e.g. a handler invoked directly in tests) — re-scoping is
        # a notification, so its absence must not break the transition it accompanies.
        with contextlib.suppress(LookupError, RuntimeError):
            await self.server.request_context.session.send_tool_list_changed()


async def connect_downstream(stack, commands: list[str]) -> dict[str, tuple[Any, types.Tool]]:
    """Connect each `--wrap` command as a stdio MCP client; map tool name -> (session, tool).
    `stack` is an AsyncExitStack owning the transports' lifetimes."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    downstream: dict[str, tuple[Any, types.Tool]] = {}
    for command in commands:
        import shlex

        argv = shlex.split(command)
        params = StdioServerParameters(command=argv[0], args=argv[1:])
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        for tool in listed.tools:
            downstream[tool.name] = (session, tool)
    return downstream


async def serve_stdio(spec: RunbookSpec, engine: LocalEngine, wrap_commands: list[str]) -> None:
    """`rh serve` entry point: wrap downstream servers, speak MCP on stdio upstream."""
    from contextlib import AsyncExitStack

    from mcp.server.stdio import stdio_server

    async with AsyncExitStack() as stack:
        downstream = await connect_downstream(stack, wrap_commands)
        runbook_server = RunbookMcpServer(spec, engine, downstream)
        read, write = await stack.enter_async_context(stdio_server())
        await runbook_server.server.run(
            read, write, runbook_server.server.create_initialization_options()
        )