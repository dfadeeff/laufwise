import json
from pathlib import Path

from laufwise.adapters.base import SimulatedAdapter, StubAdapter
from laufwise.approval.base import AutoApprovalGate
from laufwise.contract.evaluator import BuiltinEvaluator
from laufwise.engine.base import StepStatus
from laufwise.engine.local import LocalEngine
from laufwise.spec.loader import load_runbook
from laufwise.state.memory import MemoryStateProvider
from laufwise.trace.jsonl import JsonlTraceSink

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "examples" / "vendor_onboarding.yaml"


def _engine(trace_path, provider, adapter=None):
    return LocalEngine(
        provider=provider,
        evaluator=BuiltinEvaluator(),
        trace=JsonlTraceSink(trace_path),
        approval=AutoApprovalGate(),
        adapter=adapter or SimulatedAdapter(provider),
    )


def _fixture(name):
    return json.loads((ROOT / "examples" / "cases" / name).read_text())


def test_block_on_missing_docs(tmp_path):
    spec = load_runbook(RUNBOOK)
    provider = MemoryStateProvider(_fixture("missing_tax_id.json"))
    eng = _engine(tmp_path / "ep.jsonl", provider)

    results = eng.run(spec)
    eng.trace.close()

    assert results[0].status is StepStatus.OK
    assert results[0].step_id == "intake_validate"
    assert results[1].status is StepStatus.BLOCK
    assert results[1].step_id == "prepare_erp_draft"
    assert results[1].blocked_tool == "create_vendor_draft"
    assert results[1].reason == "required_docs_present=false"
    # run halted at the BLOCK — no third result
    assert len(results) == 2


def test_trace_has_two_events(tmp_path):
    spec = load_runbook(RUNBOOK)
    provider = MemoryStateProvider(_fixture("missing_tax_id.json"))
    trace_path = tmp_path / "ep.jsonl"
    eng = _engine(trace_path, provider)
    eng.run(spec)
    eng.trace.close()

    lines = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[1]["status"] == "block"
    assert lines[1]["blocked_tool"] == "create_vendor_draft"


def test_complete_case_passes(tmp_path):
    spec = load_runbook(RUNBOOK)
    provider = MemoryStateProvider(_fixture("complete.json"))
    eng = _engine(tmp_path / "ep.jsonl", provider)
    results = eng.run(spec)
    eng.trace.close()
    assert [r.status for r in results] == [StepStatus.OK, StepStatus.OK]


def test_noop_adapter_rejects_unverifiable_outcome(tmp_path):
    # The guarantee: with an adapter that does nothing, the postcondition (vendor must exist
    # in the system of record) MUST reject — the harness never accepts an unproven claim.
    spec = load_runbook(RUNBOOK)
    provider = MemoryStateProvider(_fixture("complete.json"))
    eng = _engine(tmp_path / "ep.jsonl", provider, adapter=StubAdapter())
    results = eng.run(spec)
    eng.trace.close()
    assert results[-1].status is StepStatus.REJECT
    assert results[-1].step_id == "prepare_erp_draft"


def test_approval_was_required_but_not_reached(tmp_path):
    # The BLOCK happens at precondition, before the approval gate. Approval must NOT fire.
    spec = load_runbook(RUNBOOK)
    fixture = json.loads((ROOT / "examples" / "cases" / "missing_tax_id.json").read_text())
    gate = AutoApprovalGate()
    eng = LocalEngine(
        provider=MemoryStateProvider(fixture),
        evaluator=BuiltinEvaluator(),
        trace=JsonlTraceSink(tmp_path / "ep.jsonl"),
        approval=gate,
        adapter=StubAdapter(),
    )
    eng.run(spec)
    eng.trace.close()
    assert gate.requests == []