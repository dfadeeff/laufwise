"""The DurableStore seam — run persistence, orthogonal to trace and engine.

Used by LocalEngine only: checkpoints let a crashed run resume from the last verified step.
TemporalEngine does NOT use this seam — its event history *is* the store (ARCHITECTURE §1.4).
v0 ships the protocol; the SQLite impl is the first planned backend.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from laufwise.engine.base import StepResult


@runtime_checkable
class DurableStore(Protocol):
    def checkpoint(self, run_id: str, result: StepResult) -> None:
        """Append one step ruling to the run. Append-only: rulings are never rewritten."""
        ...

    def load(self, run_id: str) -> list[StepResult]:
        """The run's rulings so far, in order — enough to resume after the last OK step."""
        ...

    def list_runs(self) -> list[str]:
        """Known run ids."""
        ...