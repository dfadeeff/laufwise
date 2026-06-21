"""Append-only JSONL trace sink. One JSON object per line, one line per emitted event."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonlTraceSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def event(self, **fields: Any) -> None:
        self._fh.write(json.dumps(fields, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()