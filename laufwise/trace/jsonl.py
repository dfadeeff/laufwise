"""Append-only JSONL trace sink. One JSON object per line, one line per emitted event.

Every line is stamped with the time it was recorded and the run it belongs to. Those are
recording concerns, not engine state, so they live here rather than in `StepResult`: the
engine stays a deterministic function of (spec, state) — replay re-drives it and gets the
same rulings — while the sink supplies the wall-clock and identity facts that make a line
self-describing once it has been shipped somewhere else.

Without them a line proves a ruling happened but not *when*, and two interleaved runs are
indistinguishable in an aggregated store. An audit record has to answer both.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonlTraceSink:
    def __init__(self, path: str | Path, context: dict[str, Any] | None = None) -> None:
        """`context` is stamped onto every event — run_id, runbook, version. Set it at the
        composition root, where the run's identity is known."""
        self.path = Path(path)
        self.context = dict(context or {})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def event(self, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), **self.context, **fields}
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()