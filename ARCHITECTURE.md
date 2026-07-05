# Laufwise — Runbook-as-Code Harness

> **Thesis.** A small Python runtime that forces a business-process agent to execute
> inside an explicit **process contract** instead of trusting a prompt. The harness is a
> **loop, not a fan-out**: every step runs the same enforced sequence, and the sequence —
> grounded in a real **System of Record** — *is* the product guarantee.
>
> **Core sentence.** Others run agents. This defines the process contract they must obey.

The per-step contract, executed in order for **every** runbook step:

```
precondition (vs real state) → tool allowlist → approval gate →
execute via adapter → postcondition (vs real state) → checkpoint + trace → next step
```

If a precondition fails, the step **blocks before any tool runs**. If a postcondition fails,
the outcome is **not accepted** even when the agent claims success. The agent's claim is
never sufficient — the harness verifies against the system of record.

---

## 1. Design decisions (and why)

### 1.1 Language: Python, small typed core
The value of this primitive is **integration surface**, not raw throughput. It is I/O- and
orchestration-bound (LLM calls, state queries, approvals). The whole adjacent ecosystem —
MCP, `temporalio`, `langfuse`, agent frameworks — is Python-first. So: **Python 3.11+,
Pydantic v2 for the spec and all wire models, a deliberately small core.** Rust buys nothing
at v0 and costs us the ecosystem; revisit only if a hot replay/indexing path ever demands it.

### 1.2 The harness is a deterministic driver; the LLM only acts *inside* `execute`
The engine that drives precondition → … → checkpoint is **plain, deterministic Python**. The
model is confined to the `execute` phase and bounded by that step's tool allowlist. This is
what makes runs auditable and replayable: the control flow is code, not a model's discretion.

### 1.3 The State Source is a first-class adapter — it is the wedge
Preconditions and postconditions are **predicates evaluated against a `StateProvider`**, never
against model output. Without this box you've built a generic agent runner; with it you've
built a verification harness. It cannot be implicit, so it is its own seam (§3.2).

### 1.4 Two clean, orthogonal seams — and the Temporal correction
The memo split **DurableStore** (resumable run state) from **TraceSink** (observability).
Correct. But one important correction from researching how Temporal actually works:

> **Temporal is not a `DurableStore` implementation.** Temporal *owns* execution state —
> event history *is* the store; you don't write checkpoints into it. So Temporal is not a
> storage backend you implement an interface against; it is an **alternate Engine backend**.

Therefore the seams are:

| Seam | What it abstracts | Default impl | Production impl |
|------|-------------------|--------------|-----------------|
| **`Engine`** | *who drives the loop & owns durability* | `LocalEngine` (in-proc loop + `DurableStore`) | `TemporalEngine` (loop → workflow+activities; history is the store) |
| **`DurableStore`** | resumable run state for the local engine | SQLite | (n/a under Temporal — history replaces it) |
| **`TraceSink`** | observability + replay (orthogonal to both engines) | JSONL (OTEL spans) | Langfuse via OTLP `/api/public/otel` |
| **`StateProvider`** | the system of record | files / SQL / REST / memory | ERP / CRM / DB / APIs |
| **`ExecutionAdapter`** | how a step's work actually runs | Raw LLM, MCP | LangGraph, Pi, AURA backend |

`TraceSink` is the easy seam: model it on **OpenTelemetry spans** and emit `gen_ai.*`
GenAI-convention attributes, so any OTLP backend (Langfuse, Phoenix, Grafana) can consume it.
JSONL is just a local OTEL exporter. Langfuse is a second exporter pointed at its OTLP endpoint.

### 1.5 Structural verification, not semantic — know the boundary

The harness's checks are **deliberately structural**: deterministic predicates over state
(entity exists, citation is real, field matches expected value). This is the trust boundary —
a deterministic check has **no false passes**.

Semantic verification — "does this evidence actually support the legal claim?" — is a
complementary layer, not a replacement. LLM-based verifiers (LLM-as-judge) are
**probabilistic**: even frontier models disagree by several percent on rubric criteria, and
that disagreement compounds across multi-criterion rubrics. Human review via the approval gate
is **authoritative** but most expensive.

| Layer | What it checks | Cost | Trust | Who |
|-------|---------------|------|-------|-----|
| **Harness postconditions** | Structural grounding (entity exists, citation is real, field matches) | Near-zero | Deterministic — no false passes | Automated |
| **LLM verifier** | Semantic correctness (does evidence support the claim?) | Expensive, reducible via batching/open models | Probabilistic — 4–5% disagreement | Automated, tunable |
| **Human review** (approval gate) | Domain judgment (is this legally/medically/financially correct?) | Most expensive | Authoritative | Domain expert |

**Do not collapse these layers.** Mixing LLM judgment into the check evaluator makes
postconditions probabilistic, and a probabilistic guarantee is not a guarantee. The harness
proves the action was **permitted and grounded** — never that the judgment was **correct**.
Semantic/domain correctness stays with the human or with a dedicated verification pipeline
that the harness feeds via TraceSink.

The harness composes with both layers above it:
- Structural postconditions **gate** whether expensive semantic verification runs at all
  (don't waste $50 on a 50-criterion LLM judge for an output that cites documents that
  don't exist).
- TraceSink output (OTEL spans) **feeds** LLM verification pipelines and annotation queues
  (LangSmith, Langfuse) as their input — the harness is infrastructure underneath
  observability, not a competing tracer.
- The approval gate is where domain experts make the **final call** on risky or ambiguous
  actions.

For the insolvency use case: the harness proves every flagged Vorgang cites a real document
and a valid InsO paragraph. Whether the citation actually demonstrates *Kenntnis* is a
semantic judgment — the harness marks it as "structurally grounded, pending review," not
"correct."

### 1.6 Inbound MCP: explicit step session + step-scoped proxy (decided 2026-07-04)

Two candidate shapes existed for exposing a runbook to MCP clients, and each alone enforces
only half the contract:

- **Steps-as-tools alone** (agent calls `begin_step`/`complete_step`): sequence and
  verification hold, but between those calls the agent works with *its own* tools — the tool
  allowlist is unenforced because the traffic never passes through the harness.
- **Transparent wrap alone** (agent calls `calendar.create_event` unchanged; laufwise
  intercepts): the allowlist binds, but the proxy must *infer* which step a call belongs to
  and *guess* when postconditions should run. Inference is model-behavior-dependent control
  flow — exactly what §1.2 forbids.

**Decision: both halves, explicitly.** `rh serve <runbook.yaml> --wrap <downstream-mcp>...`
runs one process that speaks MCP upstream (any client) and connects downstream as an MCP
client. The session is a deterministic state machine:

1. `begin_step` — gate: preconditions vs real state + approval. BLOCK halts the session.
2. While a step is active, the proxy forwards **only the current step's allowlisted
   downstream tools** — and via `tools/list_changed` they are the only tools *visible*.
   Anything else is refused and traced (defense in depth).
3. `complete_step` — verify: postconditions re-queried against the system of record, with
   `verify` retry semantics. Step N+1 cannot begin until step N verified.

The agent **requests** transitions; the engine **rules** on them — no inference anywhere, so
§1.2 (deterministic control flow) survives contact with the distribution shape. A
zero-prompt-change transparent wrap remains possible later as a degenerate single-step
runbook ("guard mode"), explicitly the weaker per-call contract.

### 1.7 Replay is ours, not Temporal's and not Langfuse's
Langfuse is **observability-only** — it cannot resume a run. Temporal can resume but only when
you adopt its execution model. We want deterministic replay **even in pure-local mode**, so a
run is recorded as an **episode log**: an append-only sequence of
`(step_id, state_snapshot_hash, decision, tool_calls, outcome)`. `rh replay` re-drives the
*same deterministic engine* over the recorded decisions and asserts identical control flow.
This gives replay for free, independent of the durability backend.

Episode logs are also raw material for **runbook improvement**: which steps block most often,
which postconditions reject, where state is systematically unavailable. Tooling to surface
these patterns is Phase 2+ — the log schema is designed to support it from day one.

---

## 2. The runbook spec (YAML/JSON)

A runbook is a **process contract**, not a prompt. Steps are ordered; each step declares its
own pre/postconditions, tool allowlist, approval policy, and execution. State queries are bound
once at the top and referenced by name.

```yaml
runbook: vendor_onboarding
version: 1
risk: medium

# Bind named state queries to providers. Checks may ONLY read through these.
state:
  vendor:      { provider: erp,   query: "vendors/by_tax_id/{tax_id}" }
  docs:        { provider: files, query: "cases/{case_id}/documents" }
  duplicates:  { provider: erp,   query: "vendors/search?name={name}" }

steps:
  - id: intake_validate
    description: Confirm the case has the documents required to onboard.
    preconditions:
      - check: docs.contains_all(["w9", "bank_letter"])
        else: "required_docs_present=false"
    tools: []                         # pure verification step, no side effects
    postconditions:
      - check: docs.count >= 2

  - id: prepare_erp_draft
    description: Create a draft vendor record in the ERP.
    preconditions:
      - check: vendor.exists == false        # don't duplicate
      - check: duplicates.count == 0
    tools: [create_vendor_draft]             # allowlist — nothing else is callable
    approval:
      required_when: "risk >= medium"
      prompt: "Create ERP draft for {name} ({tax_id})?"
    execute:
      adapter: mcp
      tool: create_vendor_draft
      args: { name: "{name}", tax_id: "{tax_id}" }
    postconditions:
      - check: vendor.exists == true         # verified against the ERP, not the agent
      - check: vendor.status == "draft"
    on_fail: halt
```

**Failure semantics.** `on_fail` applies to a REJECT (postcondition failure) only — a failed
precondition always BLOCKs and halts. Modes: `halt` (default), `goto(step_id)`, `retry(n[,
backoff])`, `compensate(step_id)`. Implemented: `halt` and `goto` — a REJECT routes to the
target step (e.g. a human-review step) and the sequence resumes from there. Routing is
deterministically bounded: no step may start more than `max_step_visits` times per run
(runbook-level, default 3); exhaustion is a traced halt and the REJECT stands. goto targets
are validated at load (must exist, must differ from the step — re-running the same step is
`retry`, which, like `compensate`, parses but is treated as halt for now, with a warning).

**Check language.** Checks are pure expressions over the named state bindings (a tiny,
sandboxed predicate DSL: `binding.field op value`, `.exists`, `.count`, `.contains_all([..])`).
Escape hatch: `check: py:my_module.my_predicate` for a registered Python callable that receives
the resolved state and returns `bool`. Either way a check is a **pure function of state
queries** — never of agent text. That purity is what makes pre/postconditions trustworthy and
replayable.

---

## 3. Core interfaces (Python protocols)

```python
# state/base.py — the wedge
class StateProvider(Protocol):
    def query(self, name: str, params: dict) -> StateView: ...
    # StateView is a read-only snapshot; checks evaluate against it and it is
    # hashed into the episode log so replay can detect state drift.

# adapters/base.py — the only place the model acts
class ExecutionAdapter(Protocol):
    def execute(self, step: Step, ctx: RunContext, tools: ToolAllowlist) -> StepOutcome: ...
    # MUST refuse any tool call outside `tools`. The allowlist is enforced here AND
    # asserted again by the engine — defense in depth.

# durable/base.py — local engine only (append-only rulings; resume from last OK step)
class DurableStore(Protocol):
    def checkpoint(self, run_id: str, result: StepResult) -> None: ...
    def load(self, run_id: str) -> list[StepResult]: ...
    def list_runs(self) -> list[str]: ...

# trace/base.py — orthogonal observability (OTEL-shaped)
class TraceSink(Protocol):
    def span(self, name: str, **otel_attrs) -> SpanCtx: ...   # gen_ai.* on LLM calls
    def event(self, name: str, **attrs) -> None: ...          # precond/postcond results

# approval/base.py — pluggable transport
class ApprovalGate(Protocol):
    def request(self, step: Step, ctx: RunContext) -> Decision: ...  # CLI / webhook / queue
```

The engine ties them together:

```python
# engine/local.py — the deterministic driver
class LocalEngine:
    def run_step(self, step: Step, ctx: RunContext) -> StepResult:
        if not self._check_all(step.preconditions, ctx):     # 1. vs StateProvider
            return BLOCK(step, reason=...)                    #    block before any tool
        tools = ToolAllowlist(step.tools)                    # 2. constrain tools
        if step.approval and self._risky(step, ctx):         # 3. approval gate
            if not self.gate.request(step, ctx).approved:
                return BLOCK(step, reason="approval_denied")
        outcome = self.adapter.execute(step, ctx, tools)     # 4. execute (model acts here)
        if not self._check_all(step.postconditions, ctx):    # 5. verify vs StateProvider
            return REJECT(step, outcome, reason=...)          #    reject even if agent "succeeded"
        self.store.checkpoint(ctx.run_id, step.id, ctx.state)# 6. checkpoint + trace
        self.trace.event("step.ok", step_id=step.id)
        return OK(step, outcome)
```

`TemporalEngine` implements the **same `Engine` interface** but maps the contract onto Temporal:
the loop becomes a `@workflow.defn`, each `execute`/`StateProvider.query` becomes an
`@activity.defn` (all I/O lives in activities — workflow code stays deterministic), and the
approval gate becomes a durable `workflow.wait_condition` released by a signal. No
`DurableStore` is used in this mode; history is the store. Same spec, same checks, same trace.

---

## 4. Module layout

```
laufwise/
  pyproject.toml
  laufwise/
    spec/        # Pydantic runbook models, loader, validator
    contract/    # checks DSL evaluator, tool_gate, pre/postcondition runners
    engine/      # base.py (Engine protocol), local.py, temporal.py (later)
    state/       # base.py + providers/{files,sql,rest,memory}.py   ← the wedge
    adapters/    # base.py + raw_llm.py, mcp.py  (langgraph.py, pi.py later)
    mcp/         # inbound MCP server: step session + scoped proxy (rh serve, §1.6)
    durable/     # base.py + sqlite.py
    trace/       # base.py + jsonl.py (OTEL exporter), otlp.py (Langfuse)
    approval/    # base.py + cli.py, webhook.py
    episode/     # append-only episode log + replay driver
    cli.py       # rh run / rh test / rh replay
  examples/
    vendor_onboarding.yaml
    cases/missing_tax_id.json
  tests/
```

CLI surface (unchanged from the memo — it's good):

```
rh run   examples/vendor_onboarding.yaml --case cases/missing_tax_id.json
rh test  examples/vendor_onboarding.yaml
rh replay runs/episode_001.jsonl
rh serve examples/booking.yaml --wrap "python calendar_mcp.py"   # inbound MCP (§1.6)
```

Target demo — the harness blocks an unsafe action *and explains why*:

```
BLOCK prepare_erp_draft
  reason: required_docs_present=false
  blocked tool call: create_vendor_draft
  trace: runs/vendor_onboarding/episode_001.jsonl
```

---

## 5. Build path

| Phase | Deliverable | Done when |
|-------|-------------|-----------|
| 0 | Spec + `LocalEngine` + memory/files `StateProvider` + checks DSL + JSONL trace + `rh run` | A precondition **blocks** a tool call and prints why, in <10 min locally |
| 1 | SQLite `DurableStore` + episode log + `rh replay` + `rh test` | A crashed run resumes; replay reproduces control flow |
| 2 | Two adapters: **Raw LLM + MCP**; CLI `ApprovalGate` | An external dev wraps an existing agent behind the contract |
| 3 | Real workflows: vendor onboarding (shows **blocking**) + insolvency (shows **grounded verification**) | Each demonstrates one half of the wedge |
| 4 | Production adapters: Langfuse OTLP `TraceSink`; `TemporalEngine` | Local harness → production path is real, no rewrite |
| 5 | (Paid, closed) hosted run history, audit reports, templates, connectors, process mining | The wedge compounds into defensibility |

### Why two proving cases
- **Vendor onboarding** is a *write-action* workflow: it shows the approval gate and the
  tool-allowlist **blocking** a premature/unsafe write. This is the dramatic demo.
- **Insolvency analysis** (the data already in `use_cases/`) is a *read/verify* workflow: low
  write-risk, but a perfect stress test for **state-grounded postconditions** — e.g. *"every
  flagged Vorgang cites a source document AND a named InsO paragraph"*, *"every § 133 flag has
  evidence of Kenntnis in correspondence, not just a contract."* It proves the verification
  engine on a real, messy document corpus where an ungrounded agent would hallucinate statutes.

Together they cover both halves of the guarantee: **block what shouldn't happen** and
**reject what can't be proven.**

---

## 6. Positioning — prevention, not detection

> **Observability tells you it went wrong. Laufwise stops it from going wrong.**

Everything in the LangSmith/Langfuse ecosystem is **post-hoc**: traces, evals, annotation
queues — all fire after the action. Laufwise is the only layer that operates **before** the
action runs. Precondition blocks, tool allowlists, and approval gates are *prevention*.
TraceSink output is *detection*. We do prevention; we feed their detection.

**What we claim:** the action was *permitted* (preconditions passed against real state) and
the outcome was *grounded* (postconditions verified against the system of record).

**What we never claim:** the judgment was *correct*. Semantic correctness is the domain
expert's call (via the approval gate) or a dedicated LLM verification pipeline's job (fed
by our TraceSink). See §1.5 for the layer model.

**Relationship to observability tools:**
- TraceSink emits OTEL spans with `gen_ai.*` attributes → any OTLP backend consumes them
- Langfuse, LangSmith, Grafana, Phoenix are all valid TraceSink targets
- We are **infrastructure underneath** their observability, not a competing tracer
- "Feeds your LangSmith/Langfuse via OTEL. Doesn't replace it."

---

## 7. Honest risks / where the real engineering is

1. **The check evaluator is the hard, valuable core.** Most of the risk lives in §2's predicate
   layer: it must be expressive enough to encode real process rules yet stay a *pure function of
   state* so it's auditable and replayable. If checks drift into arbitrary code, the wedge
   blurs. Invest here; keep it declarative with a narrow Python escape hatch.
2. **"Verify against real state" assumes the system of record is queryable and trustworthy.**
   Much enterprise reality is a messy ERP. The `StateProvider` abstraction must tolerate
   partial/stale state and make "state unavailable" a first-class blocking outcome, not a crash.
3. **Don't build five adapters.** Ship Raw LLM + MCP (MCP is the widest-reach tool standard).
   The rest are later proof-of-portability, not v0.
4. **Positioning against generic "agent guardrails."** The differentiator is a **business-process
   contract grounded in the system of record**, not prompt-level guardrails. Keep the spec
   *process-shaped* (steps, pre/postconditions, approvals), never *prompt-shaped*.
5. **Temporal/Langfuse self-host is heavyweight** (Temporal: server + datastore; Langfuse v3:
   Postgres + ClickHouse + Redis + blob store). Correct call from the memo stands: **default to
   SQLite/JSONL, make Temporal/Langfuse adapters**, so adopting the OSS primitive costs nothing.