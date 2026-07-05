import json
from pathlib import Path

import pytest

from laufwise.adapters.base import (
    SimulatedAdapter,
    StepOutcome,
    StubAdapter,
    ToolNotAllowed,
    ToolRegistryAdapter,
)
from laufwise.approval.base import AutoApprovalGate, Decision
from laufwise.contract.evaluator import BuiltinEvaluator
from laufwise.engine.base import StepStatus
from laufwise.engine.local import LocalEngine
from laufwise.spec.loader import load_runbook
from laufwise.spec.models import (
    ApprovalSpec,
    CheckSpec,
    ExecuteSpec,
    RunbookSpec,
    StateBinding,
    StepSpec,
    VerifySpec,
)
from laufwise.state.base import StateView
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


# --- the gates must bind (engine-asserted, not adapter-trusted) -----------------------


class _RecordingAdapter:
    """Records execute calls; used to prove the engine stopped BEFORE the adapter ran."""

    def __init__(self):
        self.calls = []

    def execute(self, step, allowlist):
        self.calls.append(step.id)
        return StepOutcome(ok=True)


class _DenyGate:
    def request(self, step):
        return Decision(approved=False, note="human said no")


def _single_step_spec(step: StepSpec, state: dict[str, StateBinding] | None = None) -> RunbookSpec:
    return RunbookSpec(runbook="t", risk="medium", state=state or {}, steps=[step])


def test_engine_blocks_tool_outside_allowlist(tmp_path):
    # The engine itself must refuse a declared tool missing from the step allowlist,
    # regardless of whether the adapter would have policed it.
    step = StepSpec(
        id="gated",
        tools=["allowed_tool"],
        execute=ExecuteSpec(adapter="stub", tool="forbidden_tool"),
    )
    adapter = _RecordingAdapter()
    eng = LocalEngine(
        provider=MemoryStateProvider({}),
        evaluator=BuiltinEvaluator(),
        trace=JsonlTraceSink(tmp_path / "ep.jsonl"),
        approval=AutoApprovalGate(),
        adapter=adapter,
    )
    results = eng.run(_single_step_spec(step))
    eng.trace.close()
    assert results[0].status is StepStatus.BLOCK
    assert results[0].reason == "tool_not_allowed"
    assert results[0].blocked_tool == "forbidden_tool"
    assert adapter.calls == []


def test_approval_denial_blocks_before_execute(tmp_path):
    step = StepSpec(
        id="gated",
        tools=["create_thing"],
        approval=ApprovalSpec(prompt="ok?"),  # no required_when -> always required
        execute=ExecuteSpec(adapter="stub", tool="create_thing"),
    )
    adapter = _RecordingAdapter()
    eng = LocalEngine(
        provider=MemoryStateProvider({}),
        evaluator=BuiltinEvaluator(),
        trace=JsonlTraceSink(tmp_path / "ep.jsonl"),
        approval=_DenyGate(),
        adapter=adapter,
    )
    results = eng.run(_single_step_spec(step))
    eng.trace.close()
    assert results[0].status is StepStatus.BLOCK
    assert results[0].reason == "approval_denied"
    assert results[0].blocked_tool == "create_thing"
    assert adapter.calls == []


def test_state_unavailable_is_first_class_halt(tmp_path):
    # A declared binding missing from the fixture must halt as STATE_UNAVAILABLE —
    # never crash, and never masquerade as empty state that a check could pass on.
    spec = load_runbook(RUNBOOK)
    fixture = _fixture("complete.json")
    del fixture["duplicates"]
    eng = _engine(tmp_path / "ep.jsonl", MemoryStateProvider(fixture))
    results = eng.run(spec)
    eng.trace.close()
    assert results[0].status is StepStatus.STATE_UNAVAILABLE
    assert "duplicates" in results[0].reason
    assert len(results) == 1  # run halted


# --- non-circular execution: the tool impl decides state, never the declared effect ---


def _registry_spec() -> RunbookSpec:
    step = StepSpec(
        id="create",
        tools=["create_rec"],
        execute=ExecuteSpec(adapter="registry", tool="create_rec"),
        postconditions=[CheckSpec(expr="rec.exists == true")],
    )
    return _single_step_spec(step, state={"rec": StateBinding(provider="memory")})


def test_registry_adapter_honest_tool_passes(tmp_path):
    provider = MemoryStateProvider({"rec": None})
    tools = {"create_rec": lambda p, step: p.apply({"rec": {"status": "created"}})}
    eng = _engine(tmp_path / "ep.jsonl", provider, adapter=ToolRegistryAdapter(provider, tools))
    results = eng.run(_registry_spec())
    eng.trace.close()
    assert results[0].status is StepStatus.OK


def test_registry_adapter_lying_tool_rejected(tmp_path):
    # The tool claims success but writes nothing; the postcondition re-queries state and REJECTs.
    provider = MemoryStateProvider({"rec": None})
    tools = {"create_rec": lambda p, step: StepOutcome(ok=True, note="claimed, wrote nothing")}
    eng = _engine(tmp_path / "ep.jsonl", provider, adapter=ToolRegistryAdapter(provider, tools))
    results = eng.run(_registry_spec())
    eng.trace.close()
    assert results[0].status is StepStatus.REJECT
    assert results[0].expr == "rec.exists == true"


class _RaisingAdapter:
    """Simulates an adapter refusing a disallowed call mid-execution (defense in depth)."""

    def execute(self, step, allowlist):
        raise ToolNotAllowed("agent attempted 'delete_everything' mid-execution")


def test_adapter_raised_toolnotallowed_blocks_not_crashes(tmp_path):
    # The declared tool passes the engine's own assert; the adapter's refusal of what actually
    # happened during execution must surface as a BLOCK, never as an unhandled exception.
    step = StepSpec(
        id="gated",
        tools=["create_thing"],
        execute=ExecuteSpec(adapter="stub", tool="create_thing"),
    )
    eng = LocalEngine(
        provider=MemoryStateProvider({}),
        evaluator=BuiltinEvaluator(),
        trace=JsonlTraceSink(tmp_path / "ep.jsonl"),
        approval=AutoApprovalGate(),
        adapter=_RaisingAdapter(),
    )
    results = eng.run(_single_step_spec(step))
    eng.trace.close()
    assert results[0].status is StepStatus.BLOCK
    assert results[0].reason.startswith("tool_not_allowed")


# --- verify: bounded re-verification absorbs read-after-write lag ---------------------


class _LaggingProvider(MemoryStateProvider):
    """Read-replica lag: reads of `rec` return stale None for the first N queries."""

    def __init__(self, fixture, stale_reads: int):
        super().__init__(fixture)
        self.stale_reads = stale_reads

    def query(self, name, params=None):
        if name == "rec" and self.stale_reads > 0:
            self.stale_reads -= 1
            return StateView(None)
        return super().query(name, params)


def _verify_spec(retries: int) -> RunbookSpec:
    step = StepSpec(
        id="create",
        tools=["create_rec"],
        execute=ExecuteSpec(adapter="registry", tool="create_rec"),
        postconditions=[CheckSpec(expr="rec.exists == true")],
        verify=VerifySpec(retries=retries, backoff_s=0.0),
    )
    return _single_step_spec(step, state={"rec": StateBinding(provider="memory")})


def test_verify_retry_absorbs_read_lag(tmp_path):
    # The write lands but the first post-execute read is stale; one re-verification sees it.
    provider = _LaggingProvider({"rec": None}, stale_reads=2)  # pre-read + first post-read
    tools = {"create_rec": lambda p, step: p.apply({"rec": {"status": "created"}})}
    eng = _engine(tmp_path / "ep.jsonl", provider, adapter=ToolRegistryAdapter(provider, tools))
    results = eng.run(_verify_spec(retries=1))
    eng.trace.close()
    assert results[0].status is StepStatus.OK


def test_without_verify_retry_read_lag_rejects(tmp_path):
    # Same lag, no re-verification budget: the honest-but-laggy write is (correctly, per
    # contract) rejected — which is exactly why `verify.retries` exists.
    provider = _LaggingProvider({"rec": None}, stale_reads=2)
    tools = {"create_rec": lambda p, step: p.apply({"rec": {"status": "created"}})}
    eng = _engine(tmp_path / "ep.jsonl", provider, adapter=ToolRegistryAdapter(provider, tools))
    results = eng.run(_verify_spec(retries=0))
    eng.trace.close()
    assert results[0].status is StepStatus.REJECT


# --- bindings are plumbed to the provider; on_fail modes fail loud --------------------


class _ParamRecordingProvider:
    """Asserts the engine transports the full binding to the provider untouched."""

    def __init__(self):
        self.seen: list[tuple[str, dict | None]] = []

    def query(self, name, params=None):
        self.seen.append((name, params))
        return StateView({"status": "ok"})


def test_binding_query_and_params_reach_provider(tmp_path):
    provider = _ParamRecordingProvider()
    step = StepSpec(id="s", postconditions=[CheckSpec(expr="candidate.exists == true")])
    spec = _single_step_spec(
        step,
        state={
            "candidate": StateBinding(
                provider="http",
                query="/candidates/{cid}",
                extract="data.0",
                params={"cid": "c-1"},
            )
        },
    )
    eng = _engine(tmp_path / "ep.jsonl", provider)
    results = eng.run(spec)
    eng.trace.close()
    assert results[0].status is StepStatus.OK
    name, params = provider.seen[0]
    assert name == "candidate"
    assert params == {
        "provider": "http",
        "query": "/candidates/{cid}",
        "extract": "data.0",
        "vars": {"cid": "c-1"},
    }


def test_on_fail_unknown_mode_is_rejected():
    with pytest.raises(Exception):
        StepSpec(id="s", on_fail="continue")


# --- on_fail goto: REJECT routes to a declared step, deterministically bounded --------


def test_on_fail_goto_routes_reject(tmp_path):
    # Dedup-style routing: the write step's postcondition fails -> the run routes to the
    # review step instead of halting, and the sequence resumes from the target.
    failing = StepSpec(
        id="write",
        postconditions=[CheckSpec(expr="rec.exists == true")],
        on_fail="goto(manual_review)",
    )
    review = StepSpec(id="manual_review")
    spec = RunbookSpec(
        runbook="t", state={"rec": StateBinding(provider="memory")},
        steps=[failing, review],
    )
    eng = _engine(tmp_path / "ep.jsonl", MemoryStateProvider({"rec": None}))
    results = eng.run(spec)
    eng.trace.close()
    assert [(r.step_id, r.status) for r in results] == [
        ("write", StepStatus.REJECT),
        ("manual_review", StepStatus.OK),
    ]


def test_on_fail_goto_loop_is_bounded(tmp_path):
    # A backward goto forms a loop; max_step_visits bounds it. Exhaustion is a traced halt
    # and the REJECT stands as the step's final ruling.
    prep = StepSpec(id="prep")
    write = StepSpec(
        id="write",
        postconditions=[CheckSpec(expr="rec.exists == true")],
        on_fail="goto(prep)",
    )
    spec = RunbookSpec(
        runbook="t", max_step_visits=2,
        state={"rec": StateBinding(provider="memory")},
        steps=[prep, write],
    )
    trace_path = tmp_path / "ep.jsonl"
    eng = _engine(trace_path, MemoryStateProvider({"rec": None}))
    results = eng.run(spec)
    eng.trace.close()
    assert [r.step_id for r in results] == ["prep", "write", "prep", "write"]
    assert results[-1].status is StepStatus.REJECT
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert events[-1]["status"] == "halt"
    assert "exhausted" in events[-1]["reason"]


def test_goto_never_applies_to_block(tmp_path):
    # on_fail is postcondition semantics only: a failed precondition BLOCKs and halts,
    # even when the step declares goto (CLAUDE.md: precondition fail -> BLOCK).
    gated = StepSpec(
        id="gated",
        preconditions=[CheckSpec(expr="rec.exists == true")],
        on_fail="goto(review)",
    )
    review = StepSpec(id="review")
    spec = RunbookSpec(
        runbook="t", state={"rec": StateBinding(provider="memory")},
        steps=[gated, review],
    )
    eng = _engine(tmp_path / "ep.jsonl", MemoryStateProvider({"rec": None}))
    results = eng.run(spec)
    eng.trace.close()
    assert results[0].status is StepStatus.BLOCK
    assert len(results) == 1


def test_goto_unknown_target_fails_at_load():
    with pytest.raises(Exception, match="unknown step"):
        RunbookSpec(runbook="t", steps=[StepSpec(id="a", on_fail="goto(missing)")])


def test_goto_self_target_fails_at_load():
    with pytest.raises(Exception, match="different step"):
        RunbookSpec(runbook="t", steps=[StepSpec(id="a", on_fail="goto(a)")])


def test_duplicate_step_ids_fail_at_load():
    with pytest.raises(Exception, match="duplicate step ids"):
        RunbookSpec(runbook="t", steps=[StepSpec(id="a"), StepSpec(id="a")])


def test_on_fail_unimplemented_mode_warns_and_halts(tmp_path):
    # `retry(2)` parses (it is a defined mode) but v0 warns and treats it as halt: a REJECT
    # must stop the run, never silently continue to the next step.
    with pytest.warns(UserWarning, match="treated as halt"):
        failing = StepSpec(
            id="first",
            postconditions=[CheckSpec(expr="rec.exists == true")],
            on_fail="retry(2)",
        )
    never_reached = StepSpec(id="second")
    spec = RunbookSpec(
        runbook="t", state={"rec": StateBinding(provider="memory")},
        steps=[failing, never_reached],
    )
    eng = _engine(tmp_path / "ep.jsonl", MemoryStateProvider({"rec": None}))
    results = eng.run(spec)
    eng.trace.close()
    assert results[0].status is StepStatus.REJECT
    assert len(results) == 1  # halted: `second` never ran
