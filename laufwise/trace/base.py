"""The TraceSink seam — observability, orthogonal to the engine and the durable store.

v0 ships JSONL. Later impls export OTEL spans (gen_ai.* attributes) to any OTLP backend
(Langfuse at /api/public/otel, Phoenix, Grafana Tempo) without touching core.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TraceSink(Protocol):
    def event(self, **fields: Any) -> None: ...

    def close(self) -> None: ...