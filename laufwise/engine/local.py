"""LocalEngine — the deterministic per-step driver.

This is plain, auditable Python. The order of operations IS the product guarantee
(CLAUDE.md invariant #1). The LLM gets agency only inside the execute adapter, bounded by the
step's tool allowlist. State is resolved per-step, because for real providers the system of
record changes between steps.
"""

from __future__ import annotations

import hashlib
import json
import time

from laufwise.adapters.base import ExecutionAdapter, ToolNotAllowed
from laufwise.approval.base import ApprovalGate
from laufwise.contract.evaluator import CheckEvaluator
from laufwise.engine.base import StepResult, StepStatus
from laufwise.spec.models import RunbookSpec, StepSpec
from laufwise.state.base import StateProvider, StateUnavailable, StateView
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
        # The full binding travels to the provider: query/extract/params are provider
        # vocabulary, the engine only transports them.
        return {
            name: self.provider.query(
                name,
                params={
                    "provider": binding.provider,
                    "query": binding.query,
                    "extract": binding.extract,
                    "vars": binding.params,
                },
            )
            for name, binding in spec.state.items()
        }

    @staticmethod
    def _blocked_tool(step: StepSpec) -> str | None:
        """The tool a halt prevented: the declared execute tool, else the first allowlisted."""
        if step.execute is not None and step.execute.tool is not None:
            return step.execute.tool
        return step.tools[0] if step.tools else None

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
    # run_step composes gate_step -> execute -> verify_step. Session drivers (the MCP
    # server, ARCHITECTURE §1.6) call gate_step at begin_step and verify_step at
    # complete_step, with the agent acting through the scoped proxy in between —
    # same contract functions, two drive modes.

    def gate_step(self, spec: RunbookSpec, step: StepSpec) -> tuple[StepResult | None, str | None]:
        """Phases 1-3: preconditions, declared-tool allowlist assert, approval.

        Returns (failure, pre_hash); failure is None when the gate passes.
        """
        try:
            pre_state = self._resolve_state(spec)
        except StateUnavailable as exc:
            return StepResult(step.id, StepStatus.STATE_UNAVAILABLE, reason=str(exc)), None
        pre_hash = self._state_hash(pre_state)

        # 1. preconditions (vs real state) -> BLOCK before any tool runs
        for check in step.preconditions:
            res = self.evaluator.evaluate(check.expr, pre_state)
            if not res.ok:
                return StepResult(
                    step.id, StepStatus.BLOCK,
                    reason=check.reason or res.detail,
                    expr=check.expr, blocked_tool=self._blocked_tool(step), state_hash=pre_hash,
                ), pre_hash

        # 2. tool allowlist — asserted by the engine itself; adapters also refuse
        #    (defense in depth), but the guarantee must not depend on adapter cooperation.
        if step.execute is not None and step.execute.tool is not None and step.execute.tool not in step.tools:
            return StepResult(
                step.id, StepStatus.BLOCK,
                reason="tool_not_allowed",
                blocked_tool=step.execute.tool, state_hash=pre_hash,
            ), pre_hash

        # 3. approval gate — a denial BLOCKs before the tool runs.
        if self._approval_required(spec, step):
            decision = self.approval.request(step)
            if not decision.approved:
                return StepResult(
                    step.id, StepStatus.BLOCK,
                    reason="approval_denied",
                    blocked_tool=self._blocked_tool(step), state_hash=pre_hash,
                ), pre_hash

        return None, pre_hash

    def run_step(self, spec: RunbookSpec, step: StepSpec) -> StepResult:
        failure, pre_hash = self.gate_step(spec, step)
        if failure is not None:
            return failure

        # 4. execute via adapter (the only place the model acts; allowlist-bounded).
        #    A ToolNotAllowed raised DURING execution (an adapter refusing a runtime tool
        #    attempt) is a traced halt, never a crash.
        if step.execute is not None:
            try:
                self.adapter.execute(step, allowlist=step.tools)
            except ToolNotAllowed:
                return StepResult(
                    step.id, StepStatus.BLOCK,
                    reason="tool_not_allowed",
                    blocked_tool=step.execute.tool, state_hash=pre_hash,
                )

        return self.verify_step(spec, step)

    def verify_step(self, spec: RunbookSpec, step: StepSpec) -> StepResult:
        # 5. postconditions vs state RE-RESOLVED after execution -> REJECT even if the agent
        #    claimed success. Re-resolving is what makes this a check on reality, not a claim.
        #    State lost after the action is still STATE_UNAVAILABLE: the outcome is unverified,
        #    so it is not accepted. step.verify bounds re-checks for eventually-consistent
        #    sources — re-verification only, never re-execution.
        failure = None
        for attempt in range(step.verify.retries + 1):
            if attempt:
                time.sleep(step.verify.backoff_s)
            try:
                post_state = self._resolve_state(spec)
            except StateUnavailable as exc:
                failure = StepResult(step.id, StepStatus.STATE_UNAVAILABLE, reason=str(exc))
                continue
            post_hash = self._state_hash(post_state)
            failure = None
            for check in step.postconditions:
                res = self.evaluator.evaluate(check.expr, post_state)
                if not res.ok:
                    failure = StepResult(
                        step.id, StepStatus.REJECT,
                        reason=check.reason or res.detail,
                        expr=check.expr, state_hash=post_hash,
                    )
                    break
            if failure is None:
                return StepResult(step.id, StepStatus.OK, state_hash=post_hash)
        return failure  # the last attempt's REJECT or STATE_UNAVAILABLE stands

    @staticmethod
    def route_reject(
        spec: RunbookSpec, step: StepSpec, visits: dict[str, int]
    ) -> tuple[str | None, str | None]:
        """Rule on a REJECT's on_fail: (goto_target, None) routes, (None, halt_reason) halts.

        on_fail applies to REJECT only — a BLOCK always halts. halt is the default mode
        (and the fallback for retry/compensate, which warn at spec load). goto routes to a
        declared step, bounded by spec.max_step_visits so every routing loop terminates;
        exhaustion is a traced halt and the REJECT stands as the step's final ruling.
        """
        target = step.on_fail_goto
        if target is None:
            return None, None
        if visits.get(target, 0) >= spec.max_step_visits:
            return None, (
                f"on_fail goto({target!r}) exhausted: step already started "
                f"{visits[target]} times (max_step_visits={spec.max_step_visits})"
            )
        return target, None

    def run(self, spec: RunbookSpec) -> list[StepResult]:
        results: list[StepResult] = []
        index = {step.id: i for i, step in enumerate(spec.steps)}
        visits: dict[str, int] = {}
        i = 0
        while i < len(spec.steps):
            step = spec.steps[i]
            visits[step.id] = visits.get(step.id, 0) + 1
            result = self.run_step(spec, step)
            results.append(result)
            # 6. checkpoint + trace
            self.trace.event(**result.trace_fields())
            if result.status in (StepStatus.BLOCK, StepStatus.STATE_UNAVAILABLE):
                break
            if result.status is StepStatus.REJECT:
                target, halt_reason = self.route_reject(spec, step, visits)
                if target is None:
                    if halt_reason:
                        self.trace.event(step_id=step.id, status="halt", reason=halt_reason)
                    break
                # sequence resumes from the target: goto is a jump, not a detour
                i = index[target]
                continue
            i += 1
        return results