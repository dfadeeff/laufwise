"""MemoryStateProvider — the v0 demo state source.

State is a flat JSON fixture: top-level keys map 1:1 to runbook state binding names. The
reserved `_params` key holds template variables (forward-compatible; not used in v0). This is
deliberately the simplest possible system of record so the wow moment is the BLOCK, not a SAP
connection.
"""

from __future__ import annotations

from laufwise.state.base import StateView


class MemoryStateProvider:
    def __init__(self, fixture: dict) -> None:
        self.fixture = fixture or {}

    @property
    def params(self) -> dict:
        return self.fixture.get("_params", {})

    def query(self, name: str, params: dict | None = None) -> StateView:
        return StateView(self.fixture.get(name))

    def apply(self, effect: dict) -> None:
        """Mutate the in-memory system of record. Used by SimulatedAdapter so postconditions
        can verify a real state change. Top-level keys replace the matching binding."""
        for key, value in effect.items():
            self.fixture[key] = value