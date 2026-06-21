"""LocalEngine — the deterministic per-step driver.

This is plain, auditable Python. The order of operations IS the product guarantee
(CLAUDE.md invariant #1). The LLM gets agency only inside the execute adapter, bounded by the
step's tool allowlist. State is resolved per-step, because for real providers the system of
record changes between steps.
"""

from __future__ import annotations

import hashlib
import json

from laufwise.adapters.base import ExecutionAdapter
from laufwise.approval.base import ApprovalGate
from laufwise.contract.evaluator import CheckEvaluator
from laufwise.engine.base import StepResult, StepStatus
from laufwise.spec.models import RunbookSpec, StepSpec
from laufwise.state.base import StateProvider, StateView
from laufwise.trace.base import TraceSink

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class LocalEngine:
    def __init__(
        self,
        provider: StateProvider,
        evaluator: CheckEvaluator,
        trace: TraceSink,
        approval: ApprovalGate,
        adapter: ExecutionAdapter,
    ) -> None:
        self.provider = provider
        self.evaluator = evaluator
        self.trace = trace
        self.approval = approval
        self.adapter = adapter

    # --- state resolution -------------------------------------------------
    def _resolve_state(self, spec: RunbookSpec) -> dict[str, StateView]:
        # Bindings declared in the runbook drive what is queried; if none are declared,
        # fall back to nothing (checks referencing unknown bindings raise — fail loud).
        names = list(spec.state.keys())
        return {name: self.provider.query(name) for name in names}

    @staticmethod
    def _state_hash(state: dict[str, StateView]) -> str:
        repr_ = {name: view.value for name, view in state.items()}
        blob = json.dumps(repr_, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _approval_required(self, spec: RunbookSpec, step: StepSpec) -> bool:
        if step.approval is None:
            return False
        cond = step.approval.required_when
        if not cond:
            return True
        # v0 supports only "risk >= <level>"-style gating.
        m = cond.replace(" ", "")
        for level, rank in _RISK_ORDER.items():
            if m.endswith(level):
                op = m[len("risk") : -len(level)]
                cur = _RISK_ORDER.get(spec.risk, 0)
                return {">=": cur >= rank, ">": cur > rank, "==": cur == rank}.get(op, True)
        return True

    # --- the contract -----------------------------------------------------
    def run_step(self, spec: RunbookSpec, step: StepSpec) -> StepResult:
        pre_state = self._resolve_state(spec)
        pre_hash = self._state_hash(pre_state)

        # 1. preconditions (vs real state) -> BLOCK before any tool runs
        for check in step.preconditions:
            res = self.evaluator.evaluate(check.expr, pre_state)
            if not res.ok:
                blocked = step.execute.tool if step.execute else (step.tools[0] if step.tools else None)
                return StepResult(
                    step.id, StepStatus.BLOCK,
                    reason=check.reason or res.detail,
                    expr=check.expr, blocked_tool=blocked, state_hash=pre_hash,
                )

        # 2. tool allowlist — enforced inside the adapter; nothing to do pre-execute.
        # 3. approval gate (v0 stub: records/auto-approves)
        if self._approval_required(spec, step):
            self.approval.request(step)

        # 4. execute via adapter (the only place the model acts; allowlist-bounded)
        if step.execute is not None:
            self.adapter.execute(step, allowlist=step.tools)

        # 5. postconditions vs state RE-RESOLVED after execution -> REJECT even if the agent
        #    claimed success. Re-resolving is what makes this a check on reality, not a claim.
        post_state = self._resolve_state(spec)
        post_hash = self._state_hash(post_state)
        for check in step.postconditions:
            res = self.evaluator.evaluate(check.expr, post_state)
            if not res.ok:
                return StepResult(
                    step.id, StepStatus.REJECT,
                    reason=check.reason or res.detail,
                    expr=check.expr, state_hash=post_hash,
                )

        return StepResult(step.id, StepStatus.OK, state_hash=post_hash)

    def run(self, spec: RunbookSpec) -> list[StepResult]:
        results: list[StepResult] = []
        for step in spec.steps:
            result = self.run_step(spec, step)
            results.append(result)
            # 6. checkpoint + trace
            self.trace.event(
                step_id=result.step_id,
                status=result.status.value,
                reason=result.reason,
                expr=result.expr,
                blocked_tool=result.blocked_tool,
                state_hash=result.state_hash,
            )
            if result.status is StepStatus.BLOCK:
                break
            if result.status is StepStatus.REJECT and step.on_fail == "halt":
                break
        return results