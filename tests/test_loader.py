from pathlib import Path

import pytest

from laufwise.spec.loader import RunbookValidationError, load_runbook

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "vendor_onboarding.yaml"


def test_loads_runbook_and_steps():
    spec = load_runbook(EXAMPLE)
    assert spec.runbook == "vendor_onboarding"
    assert [s.id for s in spec.steps] == ["intake_validate", "prepare_erp_draft"]


def test_check_aliases_parse():
    spec = load_runbook(EXAMPLE)
    gate = spec.steps[1].preconditions[0]
    assert gate.expr == 'docs.contains_all(["w9", "bank_letter"])'
    assert gate.reason == "required_docs_present=false"


# --- checks are validated at load, not discovered mid-run ---------------------


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rb.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_undeclared_binding_in_postcondition_fails_at_load(tmp_path):
    # The audit case: a typo'd binding in a POSTcondition is not reached until after the
    # step's tool has run. Caught at load, the tool never fires at all.
    path = _write(tmp_path, """
runbook: typo
state:
  vendor: { provider: memory }
steps:
  - id: s1
    tools: [write_thing]
    execute: { adapter: stub, tool: write_thing }
    postconditions:
      - check: vendr.status == "draft"
""")
    with pytest.raises(RunbookValidationError, match="undeclared state binding 'vendr'"):
        load_runbook(path)


def test_unparseable_check_fails_at_load(tmp_path):
    path = _write(tmp_path, """
runbook: bad_syntax
state:
  docs: { provider: memory }
steps:
  - id: s1
    preconditions:
      - check: docs.count >= 1 and docs.count < 9
""")
    with pytest.raises(RunbookValidationError, match="connective"):
        load_runbook(path)


def test_all_faults_are_reported_together(tmp_path):
    # Fixing a config file one error per run is a miserable loop — report them all at once.
    path = _write(tmp_path, """
runbook: many_faults
state:
  docs: { provider: memory }
steps:
  - id: s1
    preconditions:
      - check: dcs.count >= 1
      - check: docs.status == draft
    postconditions:
      - check: docs.count >= 1 and docs.exists == true
""")
    with pytest.raises(RunbookValidationError) as caught:
        load_runbook(path)
    message = str(caught.value)
    assert "3 invalid checks" in message
    assert "'dcs'" in message
    assert "step 's1' precondition" in message
    assert "step 's1' postcondition" in message


def test_valid_runbook_with_bindings_loads_clean(tmp_path):
    path = _write(tmp_path, """
runbook: fine
state:
  docs: { provider: memory }
  vendor: { provider: memory }
steps:
  - id: s1
    preconditions:
      - check: docs.count >= 1
    postconditions:
      - check: vendor.status == "draft"
""")
    assert load_runbook(path).runbook == "fine"


def test_evaluator_without_validate_opts_out(tmp_path):
    # The CheckEvaluator seam stays orthogonal: an evaluator with different rules (CEL) is
    # not held to the built-in DSL's grammar at load.
    class ForeignEvaluator:
        def evaluate(self, expr, state):  # pragma: no cover - never called here
            raise AssertionError

    path = _write(tmp_path, """
runbook: cel_style
state:
  docs: { provider: memory }
steps:
  - id: s1
    preconditions:
      - check: docs.count >= 1 && docs.exists
""")
    assert load_runbook(path, evaluator=ForeignEvaluator()).runbook == "cel_style"