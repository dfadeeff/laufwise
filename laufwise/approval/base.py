"""Approval gate seam. v0 ships an auto-approving stub; real impls pause for a human
(CLI prompt, webhook, queue) and, under TemporalEngine, become a durable wait_condition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from laufwise.spec.models import StepSpec


@dataclass
class Decision:
    approved: bool
    note: str = ""


@runtime_checkable
class ApprovalGate(Protocol):
    def request(self, step: StepSpec) -> Decision: ...


class AutoApprovalGate:
    """v0 stub — records that approval was required and auto-approves."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def request(self, step: StepSpec) -> Decision:
        self.requests.append(step.id)
        return Decision(approved=True, note="auto-approved (v0 stub)")