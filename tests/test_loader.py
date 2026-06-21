from pathlib import Path

from laufwise.spec.loader import load_runbook

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