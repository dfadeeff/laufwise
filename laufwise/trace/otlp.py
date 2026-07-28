"""OtlpTraceSink — emit harness events as OpenTelemetry spans to any OTLP backend.

Concrete proof of the positioning: Laufwise *feeds* your observability stack (Langfuse,
LangSmith, Grafana Tempo, Phoenix) — it does not replace it. This is a drop-in swap for
JsonlTraceSink: it implements the same TraceSink protocol (`event(**fields)` / `close()`), so
the engine uses it unchanged.

Step events carry `laufwise.*` attributes (step semantics) plus the `gen_ai.system` marker so
GenAI-aware backends attribute the span; per-LLM-call `gen_ai.*` request/usage attributes
arrive with the LLM execution adapter.

Requires the optional `otel` extra:  pip install "laufwise[otel]"
Configure via standard env vars (read by the OTLP exporter):
    OTEL_EXPORTER_OTLP_ENDPOINT   e.g. https://cloud.langfuse.com/api/public/otel
    OTEL_EXPORTER_OTLP_HEADERS    e.g. Authorization=Basic <base64(public:secret)>
"""

from __future__ import annotations

import json
from typing import Any

try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

# Statuses that should mark the span as an error in the backend.
_ERROR_STATUSES = {"block", "reject", "error"}


def _coerce(value: Any) -> Any:
    """OTEL attributes accept str/bool/int/float (and sequences). Everything else is JSON."""
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, default=str)


class OtlpTraceSink:
    def __init__(
        self,
        service_name: str = "laufwise",
        endpoint: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """`context` mirrors JsonlTraceSink: run identity stamped onto every span, so a run
        is reconstructable in the backend rather than only in the local episode file."""
        if not _OTEL_AVAILABLE:
            raise ImportError(
                "OtlpTraceSink requires the opentelemetry packages. "
                "Install with: pip install 'laufwise[otel]'"
            )

        self.context = dict(context or {})
        resource = Resource.create({"service.name": service_name})
        self._provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("laufwise")

    def event(self, **fields: Any) -> None:
        """Drop-in match for the TraceSink protocol — no positional span name."""
        span = self._tracer.start_span(f"laufwise.step.{fields.get('status', 'event')}")
        span.set_attribute("gen_ai.system", "laufwise")  # source marker for GenAI backends
        for key, value in {**self.context, **fields}.items():
            if value is None:
                continue
            span.set_attribute(f"laufwise.{key}", _coerce(value))
        if fields.get("status") in _ERROR_STATUSES:
            span.set_status(Status(StatusCode.ERROR, str(fields.get("reason") or "")))
        span.end()

    def flush(self) -> None:
        self._provider.force_flush()

    def close(self) -> None:
        self._provider.force_flush()
        self._provider.shutdown()