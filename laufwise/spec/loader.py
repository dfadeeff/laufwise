"""Load and validate a runbook YAML/JSON file into a RunbookSpec."""

from __future__ import annotations

from pathlib import Path

import yaml

from laufwise.spec.models import RunbookSpec


def load_runbook(path: str | Path) -> RunbookSpec:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RunbookSpec.model_validate(raw)