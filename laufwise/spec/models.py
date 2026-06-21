"""Pydantic v2 models for the runbook spec.

A runbook is a *process contract*, not a prompt. Steps are ordered; each declares its own
pre/postconditions, tool allowlist, approval policy, and execution. State queries are bound
once at the top and referenced by name. Domain knowledge lives here in user data, never in
the engine.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
    """Binds a named state query to a provider. Checks may ONLY read through these."""

    provider: str = "memory"
    query: str | None = None


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


class StepSpec(BaseModel):
    id: str
    description: str = ""
    preconditions: list[CheckSpec] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    approval: ApprovalSpec | None = None
    execute: ExecuteSpec | None = None
    postconditions: list[CheckSpec] = Field(default_factory=list)
    # Defined failure modes (CLAUDE.md): halt (default) | retry | goto | compensate.
    # v0 implements `halt` only; others parse but are treated as halt with a warning.
    on_fail: str = "halt"


class RunbookSpec(BaseModel):
    runbook: str
    version: int = 1
    risk: str = "low"
    state: dict[str, StateBinding] = Field(default_factory=dict)
    steps: list[StepSpec]