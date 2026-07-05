"""Pydantic v2 models for the runbook spec.

A runbook is a *process contract*, not a prompt. Steps are ordered; each declares its own
pre/postconditions, tool allowlist, approval policy, and execution. State queries are bound
once at the top and referenced by name. Domain knowledge lives here in user data, never in
the engine.
"""

from __future__ import annotations

import re
import warnings

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Defined failure modes (CLAUDE.md): halt | retry(n[, backoff]) | goto(step) | compensate(step).
_ON_FAIL = re.compile(r"^(halt|retry\([^)]*\)|goto\(\w+\)|compensate\(\w+\))$")
_GOTO = re.compile(r"^goto\((\w+)\)$")
_IMPLEMENTED_ON_FAIL = ("halt", "goto(")


class CheckSpec(BaseModel):
    """A single pre/postcondition. `expr` is a pure predicate over state bindings.

    YAML form:
        - check: docs.contains_all(["w9", "bank_letter"])
          else: "required_docs_present=false"
    """

    model_config = ConfigDict(populate_by_name=True)

    expr: str = Field(alias="check")
    reason: str | None = Field(default=None, alias="else")


class StateBinding(BaseModel):
    """Binds a named state query to a provider. Checks may ONLY read through these.

    `query` is provider-specific (e.g. a URL for the http provider), `extract` narrows the
    response (dotted path into JSON), `params` are template variables for the query. The
    engine passes all three through to the provider — bindings are data, resolution is the
    provider's job.
    """

    provider: str = "memory"
    query: str | None = None
    extract: str | None = None
    params: dict = Field(default_factory=dict)


class ApprovalSpec(BaseModel):
    required_when: str | None = None
    prompt: str | None = None


class ExecuteSpec(BaseModel):
    adapter: str = "stub"
    tool: str | None = None
    args: dict = Field(default_factory=dict)
    # Demo-only: the state change this tool would cause, so a SimulatedAdapter can stand in
    # for a real system of record. Real adapters call real tools; postconditions re-query the
    # real state regardless. Lives in the runbook (data), never in the engine.
    effect: dict = Field(default_factory=dict)


class VerifySpec(BaseModel):
    """Bounded re-verification of postconditions. Real systems of record are eventually
    consistent (a calendar write may not be readable for a few seconds); without this, an
    honest action can be falsely REJECTed. retries=0 (default) checks exactly once. This is
    NOT `on_fail: retry` — nothing is re-executed, the same postconditions are re-checked
    against freshly re-resolved state."""

    retries: int = Field(default=0, ge=0)
    backoff_s: float = Field(default=1.0, ge=0)


class StepSpec(BaseModel):
    id: str
    description: str = ""
    preconditions: list[CheckSpec] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    approval: ApprovalSpec | None = None
    execute: ExecuteSpec | None = None
    postconditions: list[CheckSpec] = Field(default_factory=list)
    verify: VerifySpec = Field(default_factory=VerifySpec)
    # Defined failure modes (CLAUDE.md): halt (default) | retry | goto | compensate.
    # Implemented: halt, goto(step) — a REJECT routes to the target step (e.g. a human-review
    # step), bounded by RunbookSpec.max_step_visits. retry/compensate parse but are treated
    # as halt with a warning. on_fail never applies to BLOCK: a failed precondition halts.
    on_fail: str = "halt"

    @field_validator("on_fail")
    @classmethod
    def _known_on_fail(cls, v: str) -> str:
        v = v.strip()
        if not _ON_FAIL.match(v):
            raise ValueError(
                f"on_fail must be halt | retry(...) | goto(step) | compensate(step), got {v!r}"
            )
        if not v.startswith(_IMPLEMENTED_ON_FAIL):
            warnings.warn(
                f"on_fail={v!r} is parsed but not implemented yet — treated as halt",
                stacklevel=2,
            )
        return v

    @property
    def on_fail_goto(self) -> str | None:
        """The goto target step id when on_fail is goto(step_id); None for every other mode."""
        m = _GOTO.match(self.on_fail)
        return m.group(1) if m else None


class RunbookSpec(BaseModel):
    runbook: str
    version: int = 1
    risk: str = "low"
    # Termination bound for on_fail goto routing: no step may START more than this many
    # times in one run. A route that would exceed it halts the run (traced), so every
    # routing loop terminates — deterministically, in the engine, per invariant #1.
    max_step_visits: int = Field(default=3, ge=1)
    state: dict[str, StateBinding] = Field(default_factory=dict)
    steps: list[StepSpec]

    @model_validator(mode="after")
    def _steps_form_a_valid_graph(self) -> RunbookSpec:
        # goto made step ids into routing targets, so they must be unique and every
        # target must exist — a dangling route is a config error, caught at load.
        ids = [step.id for step in self.steps]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate step ids: {duplicates}")
        known = set(ids)
        for step in self.steps:
            target = step.on_fail_goto
            if target is None:
                continue
            if target == step.id:
                raise ValueError(
                    f"step {step.id!r}: goto must target a different step — "
                    "re-running the same step is retry(...)"
                )
            if target not in known:
                raise ValueError(
                    f"step {step.id!r}: on_fail goto targets unknown step {target!r}"
                )
        return self