"""Execution adapter seam — the ONLY place the model acts.

An adapter MUST refuse any tool call outside the step's allowlist (defense in depth: the
engine constrains, the adapter enforces). v0 ships a stub that performs no real work, so the
demo's value comes entirely from the contract, not from execution. Raw LLM + MCP adapters
come next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from laufwise.spec.models import StepSpec


class ToolNotAllowed(Exception):
    """Raised when execution attempts a tool outside the step allowlist."""


@dataclass
class StepOutcome:
    ok: bool
    note: str = ""
    data: dict | None = None


@runtime_checkable
class ExecutionAdapter(Protocol):
    def execute(self, step: StepSpec, allowlist: list[str]) -> StepOutcome: ...


class StubAdapter:
    """v0 stub — does nothing, but still enforces the allowlist contract.

    With this adapter a step whose postcondition requires a real state change will REJECT —
    which is correct: nothing happened, so the harness must not accept the agent's claim.
    """

    def execute(self, step: StepSpec, allowlist: list[str]) -> StepOutcome:
        tool = step.execute.tool if step.execute else None
        if tool is not None and tool not in allowlist:
            raise ToolNotAllowed(f"{tool!r} not in step allowlist {allowlist}")
        return StepOutcome(ok=True, note="execute skipped (v0 stub)")


class SimulatedAdapter:
    """Demo adapter — applies the step's declared `effect` to a MemoryStateProvider, standing
    in for a real tool writing to a real system of record. Still enforces the allowlist. The
    postcondition independently re-queries state; it never trusts this adapter's return value.
    """

    def __init__(self, provider) -> None:
        self.provider = provider

    def execute(self, step: StepSpec, allowlist: list[str]) -> StepOutcome:
        tool = step.execute.tool if step.execute else None
        if tool is not None and tool not in allowlist:
            raise ToolNotAllowed(f"{tool!r} not in step allowlist {allowlist}")
        effect = step.execute.effect if step.execute else {}
        if effect and hasattr(self.provider, "apply"):
            self.provider.apply(effect)
        return StepOutcome(ok=True, note=f"applied effect={effect}" if effect else "no effect")


class ToolRegistryAdapter:
    """Executes registered Python tool implementations against the provider-backed store.

    Non-circular by construction: the runbook's declared `effect` is ignored — what lands in
    state is whatever the tool implementation actually does, and the postcondition re-queries
    state to find out. An implementation that claims success but writes nothing is REJECTed
    by the engine, exactly like a real tool whose write did not land.
    """

    def __init__(self, provider, tools: dict[str, Callable[..., StepOutcome | None]]) -> None:
        self.provider = provider
        self.tools = tools

    def execute(self, step: StepSpec, allowlist: list[str]) -> StepOutcome:
        tool = step.execute.tool if step.execute else None
        if tool is None:
            return StepOutcome(ok=True, note="no tool declared")
        if tool not in allowlist:
            raise ToolNotAllowed(f"{tool!r} not in step allowlist {allowlist}")
        impl = self.tools.get(tool)
        if impl is None:
            return StepOutcome(ok=False, note=f"no implementation registered for {tool!r}")
        outcome = impl(self.provider, step)
        if isinstance(outcome, StepOutcome):
            return outcome
        return StepOutcome(ok=True, note=f"executed {tool!r}")
