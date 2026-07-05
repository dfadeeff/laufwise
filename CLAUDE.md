# CLAUDE.md — Laufwise

Guidance for any coding agent working in this repo. Read `ARCHITECTURE.md` for the full
design. This file is the short, binding set of principles. **Follow best design practices and
the invariants below. When a change would violate an invariant, stop and flag it — don't work
around it.**

## What this is

Laufwise is an open-source **runbook-as-code harness** for business-process agents: a small
Python runtime that forces an agent to execute inside an explicit **process contract** instead
of trusting a prompt. The harness is a **loop, not a fan-out**. Every step runs the same
enforced sequence, and the order *is* the product guarantee:

```
precondition (vs real state) → tool allowlist → approval gate →
execute via adapter → postcondition (vs real state) → checkpoint + trace → next step
```

Core sentence: *Others run agents. This defines the process contract they must obey.*

## Invariants (do not break)

1. **The engine is deterministic; the LLM only acts inside `execute`.** Control flow
   (check precondition → gate tools → approval → verify postcondition → checkpoint) is plain,
   auditable Python — never model discretion. The model gets agency only in the `execute`
   phase, bounded by that step's tool allowlist.
2. **Checks are pure functions of state.** Pre/postconditions evaluate against a
   `StateProvider` (the system of record), **never** against agent text. The agent's claim is
   never sufficient. A check must be reproducible and side-effect-free.
3. **Verify structure, not semantics.** The harness proves a claim is *grounded* (cited
   document exists, § is a real statute, evidence of the required type is present, state field
   has the expected value) — not that it is *correct in judgment*. Semantic/legal/business
   correctness stays with the human. Never let a check silently assert truth it can't ground.
4. **State unavailable is a first-class BLOCK, not a crash.** Distinguish *temporarily
   unavailable* (retryable) from *not implemented for this provider* (halt as config error).
5. **The seams are protocols, and they stay orthogonal.** `Engine`, `StateProvider`,
   `ExecutionAdapter`, `DurableStore`, `TraceSink`, `ApprovalGate`, `CheckEvaluator`. Don't
   collapse them or wire engine logic into an adapter. Defaults: `LocalEngine` + SQLite
   `DurableStore` + JSONL (OTEL) `TraceSink`. Temporal is an alternate **`Engine`**, not a
   `DurableStore` (its event history *is* the store). Langfuse is a `TraceSink` (OTLP).
6. **Replay is ours.** A run is an append-only episode log of
   `(step_id, state_hash, decision, tool_calls, outcome)`. Replay re-drives the *same engine*.
   Keep it independent of any durability/observability backend.

## Design practices

- **Small, typed core.** Python 3.11+, Pydantic v2 for all specs and wire models. Prefer
  protocols (`typing.Protocol`) for every seam so impls are swappable and testable.
- **The check evaluator is the make-or-break component — design it behind a
  `CheckEvaluator` protocol.** v0 may ship a tiny built-in DSL (`.exists`, `.count`,
  `.contains_all`, `field op value`) with an explicit `py:` escape hatch, but the evaluator
  must be replaceable by **CEL** (Common Expression Language; `cel-python`) without touching
  the engine. The `py:` escape hatch must feel like a conscious decision to leave the sandbox,
  never the default path.
- **Explicit failure semantics in the spec.** `on_fail` is not free text. Defined modes:
  `halt` (default), `retry(n, backoff)`, `goto(step_id)`, `compensate(step_id)`. Precondition
  fail → BLOCK. Postcondition fail → REJECT, then apply `on_fail`. Document the chosen mode.
- **MCP is the primary distribution channel, both directions.** Inbound: expose runbook steps
  as MCP tools so any MCP client (Claude, Cursor, any agent) can be the execution backend.
  Outbound: let state queries and `execute` steps call MCP servers, so existing MCP
  integrations become `StateProvider`s / execution targets for free. Ship Raw LLM + MCP
  adapters first; LangGraph/Pi/AURA later as portability proof.
- **Process-shaped, not prompt-shaped.** The spec describes steps, pre/postconditions,
  approvals, allowlists — never prompt guardrails. The differentiator is a business-process
  contract grounded in the system of record, not content safety.
- Keep dependencies minimal; don't hard-depend on Temporal/Langfuse. Default to SQLite/JSONL so
  adopting the OSS primitive costs nothing.

## Current open decisions (record the call when made)

- **A. Check DSL scope at v0:** tiny built-in DSL + `py:` escape hatch now, CEL as the drop-in
  upgrade — *current lean: tiny-DSL-behind-protocol, CEL-ready*.
- **B. Runbook control flow:** pure sequence at v0 ("the order is the guarantee"), with a
  documented path to `if/match/retry`/branching — *current lean: pure sequence v0*. The
  insolvency case may force branching (e.g. "if § 133 flag found, run deeper Kenntnis
  analysis"); add it deliberately, not by accident.
- **C. Inbound MCP shape: DECIDED (2026-07-04) — explicit step session + step-scoped proxy.**
  `rh serve <runbook> --wrap <mcp-server>...` exposes `begin_step`/`complete_step` control
  tools plus ONLY the current step's allowlisted downstream tools (re-scoped via
  `tools/list_changed` on every transition). The agent *requests* transitions; the engine
  *rules* on them. Rejected alternatives: steps-as-tools alone (allowlist unenforced — tool
  traffic never passes through the harness) and transparent wrap alone (step boundaries would
  be *inferred* from traffic — model-dependent control flow, violating invariant #1). A
  zero-prompt-change transparent wrap may ship later as a degenerate single-step runbook,
  explicitly labeled the weaker per-call contract. See ARCHITECTURE.md §1.6.

## Repo conventions

- `use_cases/` is **gitignored** — local, private case data (e.g. the insolvency corpus). Never
  commit it; never assume others have it. Code that reads it must take a configurable path and
  fail gracefully when absent.
- `spike/` holds de-risking spikes (not production code), but it must still honor the
  invariants — a spike that cheats the wedge proves nothing.
- `ARCHITECTURE.md` is the source of truth for design; update it when a decision changes.

## Naming note

"Runbook" carries SRE/incident-response connotations (PagerDuty, RunbookAI). Our usage means
*the formal procedure for a business process*. Prefer **"process contract"** in
developer-facing copy where confusion is likely; own the redefinition deliberately.