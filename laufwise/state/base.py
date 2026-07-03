"""The State Source seam — the wedge.

Checks evaluate against a StateView (a read-only snapshot of the system of record), never
against agent text. StateView is intentionally small: the accessors here are the vocabulary
the check DSL exposes. The same abstraction must serve structured state (ERP row) and, later,
document-grounded state (evidence bundles) — see ARCHITECTURE.md.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, runtime_checkable


class StateUnavailable(Exception):
    """Raised by a StateProvider when a declared binding cannot be resolved (source down,
    binding missing from the fixture). The engine turns this into a first-class
    STATE_UNAVAILABLE halt — it must never masquerade as empty state a check could pass on."""


class StateView:
    """A read-only view over one resolved state binding."""

    def __init__(self, value: Any) -> None:
        self.value = value

    @property
    def exists(self) -> bool:
        if self.value is None:
            return False
        if isinstance(self.value, (list, dict, str)):
            return len(self.value) > 0
        return True

    @property
    def count(self) -> int:
        if self.value is None:
            return 0
        if isinstance(self.value, (list, dict, str)):
            return len(self.value)
        return 1

    def contains_all(self, items: list[Any]) -> bool:
        if not isinstance(self.value, list):
            return False
        return all(item in self.value for item in items)

    def get_field(self, name: str) -> Any:
        if isinstance(self.value, dict):
            return self.value.get(name)
        return getattr(self.value, name, None)

    def state_hash(self) -> str:
        blob = json.dumps(self.value, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@runtime_checkable
class StateProvider(Protocol):
    """Resolves a named state binding to a StateView. The system of record adapter."""

    def query(self, name: str, params: dict | None = None) -> StateView: ...