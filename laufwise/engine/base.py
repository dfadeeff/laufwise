"""Engine result types. The Engine itself is a protocol so LocalEngine and (later)
TemporalEngine are interchangeable backends for the same per-step contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StepStatus(str, Enum):
    OK = "ok"          # all conditions satisfied against real state
    # a gate refused the step: failed precondition, tool outside the allowlist, or approval
    # denied — in every case the declared tool was not executed by the engine. Adapters that
    # refuse mid-execution (ToolNotAllowed) MUST do so before causing side effects, or the
    # BLOCK they trigger would hide an unverified partial write.
    BLOCK = "block"
    REJECT = "reject"  # postcondition failed -> outcome not accepted despite agent claim
    # a declared state binding could not be resolved -> checks cannot run, so the step
    # halts as its own outcome (CLAUDE.md invariant #4: first-class, never a crash, and
    # never silently evaluated as empty state)
    STATE_UNAVAILABLE = "state_unavailable"


@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    reason: str | None = None
    expr: str | None = None
    blocked_tool: str | None = None
    state_hash: str | None = None

    def trace_fields(self) -> dict[str, str | None]:
        """The canonical trace-event shape for a step ruling — every driver (engine.run,
        MCP session) emits exactly this, so episodes stay comparable across drive modes."""
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "reason": self.reason,
            "expr": self.expr,
            "blocked_tool": self.blocked_tool,
            "state_hash": self.state_hash,
        }