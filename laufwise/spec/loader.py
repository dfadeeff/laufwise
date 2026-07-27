"""Load and validate a runbook YAML/JSON file into a RunbookSpec.

Loading validates two things: the schema (Pydantic) and **every check expression** against
the runbook's declared state bindings.

The second one matters more than it looks. A check that cannot be parsed, or that reads a
binding the runbook never declared, is a config error — and a config error discovered
mid-run is the worst possible time to discover it. A typo in a *post*condition is not
reached until after the step's tool has already run, so the write lands and the ruling that
should have judged it never happens. Validation at load moves that fault to before the run
starts, where it costs nothing.

Check validation goes through the CheckEvaluator seam, so a runbook written for a different
evaluator (CEL) is validated by that evaluator's rules rather than the built-in DSL's. An
evaluator that offers no `validate` simply opts out — the engine still rules at runtime
(invariant #5: the seams stay orthogonal).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from laufwise.contract.evaluator import BuiltinEvaluator, CheckError
from laufwise.spec.models import RunbookSpec


class RunbookValidationError(ValueError):
    """The runbook's checks are invalid. Every fault is reported at once — fixing a config
    file one error per run is a miserable loop."""


def _validate_checks(spec: RunbookSpec, evaluator: object) -> None:
    validate = getattr(evaluator, "validate", None)
    if validate is None:
        return  # this evaluator offers no static validation; runtime rulings still apply

    declared = set(spec.state)
    problems: list[str] = []
    for step in spec.steps:
        phases = (
            ("precondition", step.preconditions),
            ("postcondition", step.postconditions),
        )
        for phase, checks in phases:
            for check in checks:
                try:
                    validate(check.expr, declared)
                except (CheckError, NotImplementedError, ValueError) as exc:
                    problems.append(f"  step {step.id!r} {phase}: {exc}")

    if problems:
        raise RunbookValidationError(
            f"runbook {spec.runbook!r} has {len(problems)} invalid "
            f"check{'s' if len(problems) > 1 else ''}:\n" + "\n".join(problems)
        )


def load_runbook(path: str | Path, evaluator: object | None = None) -> RunbookSpec:
    """Parse, schema-validate, and check-validate a runbook.

    `evaluator` selects whose rules the check expressions are validated against; it defaults
    to the built-in DSL, matching the engine's default. Pass the same evaluator the engine
    will run with, or the load-time guarantee does not cover it.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = RunbookSpec.model_validate(raw)
    _validate_checks(spec, evaluator or BuiltinEvaluator())
    return spec