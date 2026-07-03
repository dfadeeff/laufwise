"""Pydantic v2 models for the runbook spec.

A runbook is a *process contract*, not a prompt. Steps are ordered; each declares its own
pre/postconditions, tool allowlist, approval policy, and execution. State queries are bound
once at the top and referenced by name. Domain knowledge lives here in user data, never in
the engine.
"""

from __future__ import annotations

import re
import warnings

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Defined failure modes (CLAUDE.md): halt | retry(n[, backoff]) | goto(step) | compensate(step).
_ON_FAIL = re.compile(r"^(halt|retry\([^)]*\)|goto\(\w+\)|compensate\(\w+\))$")


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
    # v0 implements `halt` only; others parse but are treated as halt with a warning.
    on_fail: str = "halt"

    @field_validator("on_fail")
    @classmethod
    def _known_on_fail(cls, v: str) -> str:
        v = v.strip()
        if not _ON_FAIL.match(v):
            raise ValueError(
                f"on_fail must be halt | retry(...) | goto(step) | compensate(step), got {v!r}"
            )
        if v != "halt":
            warnings.warn(
                f"on_fail={v!r} is parsed but not implemented in v0 — treated as halt",
                stacklevel=2,
            )
        return v


class RunbookSpec(BaseModel):
    runbook: str
    version: int = 1
    risk: str = "low"
    state: dict[str, StateBinding] = Field(default_factory=dict)
    steps: list[StepSpec]