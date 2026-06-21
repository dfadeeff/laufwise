"""Engine result types. The Engine itself is a protocol so LocalEngine and (later)
TemporalEngine are interchangeable backends for the same per-step contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StepStatus(str, Enum):
    OK = "ok"          # all conditions satisfied against real state
    BLOCK = "block"    # precondition failed -> stopped BEFORE any tool ran
    REJECT = "reject"  # postcondition failed -> outcome not accepted despite agent claim


@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    reason: str | None = None
    expr: str | None = None
    blocked_tool: str | None = None
    state_hash: str | None = None