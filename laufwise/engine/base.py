"""Engine result types. The Engine itself is a protocol so LocalEngine and (later)
TemporalEngine are interchangeable backends for the same per-step contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum


def tool_call_record(tool: str | None, args: dict | None, ok: bool) -> dict:
    """One line of the receipt for a tool call. Shared by every driver (engine.run, the MCP
    session) so a call looks the same however it reached the harness.

    Arguments are recorded as a hash, not verbatim. The audit question is "was THIS call
    made with THESE arguments" — a digest answers it and can be recomputed by anyone holding
    the inputs, without the episode log becoming a place customer data, credentials, or PII
    accumulate. Tool names are kept: they are the allowlist vocabulary and carry no payload.
    """
    blob = json.dumps(args or {}, sort_keys=True, default=str)
    return {
        "tool": tool,
        "args_hash": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16],
        "ok": ok,
    }


class StepStatus(StrEnum):
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
    # the check itself is broken (unparseable, undeclared binding, incomparable values), so
    # no verdict about the world can be drawn from it. Same reasoning as STATE_UNAVAILABLE:
    # "the check could not run" is NOT "the check passed", and it is not a crash either — a
    # step whose tool already ran must still produce a traced ruling. Never routed by
    # on_fail: a broken check is a config fault, and retrying or rerouting cannot fix it.
    CHECK_ERROR = "check_error"


@dataclass
class StepResult:
    """One step's ruling, and the evidence behind it.

    The evidence is the point. A receipt that records only the verdict proves the harness
    had an opinion; a receipt that records the state hash BEFORE the action, what was
    called, and the state hash AFTER proves what the action did to the system of record
    (ARCHITECTURE §1.7 — the episode tuple is (step_id, state_hash, decision, tool_calls,
    outcome)). Both hashes, because before→action→after is the chain — one hash alone
    cannot show that anything changed.
    """

    step_id: str
    status: StepStatus
    reason: str | None = None
    expr: str | None = None
    blocked_tool: str | None = None
    # State at the gate, i.e. what the preconditions were judged against. Present whenever
    # state resolved at all — including on a BLOCK, where it is the evidence for refusing.
    state_hash_before: str | None = None
    # State re-resolved after execution, i.e. what the postconditions were judged against.
    # None when the step never got that far, which is itself meaningful: no action landed.
    state_hash_after: str | None = None
    # Tool calls the harness actually saw for this step. None = not recorded; [] = a step
    # that legitimately called nothing (a pure verification step).
    tool_calls: list[dict] | None = None

    def trace_fields(self) -> dict[str, object]:
        """The canonical trace-event shape for a step ruling — every driver (engine.run,
        MCP session) emits exactly this, so episodes stay comparable across drive modes."""
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "reason": self.reason,
            "expr": self.expr,
            "blocked_tool": self.blocked_tool,
            "state_hash_before": self.state_hash_before,
            "state_hash_after": self.state_hash_after,
            "tool_calls": self.tool_calls,
        }