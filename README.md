# Laufwise

> Observability tells you it went wrong. Laufwise stops it from going wrong.

Runbook-as-code harness for business-process agents. Define a process contract
(preconditions, tool gates, approvals, postconditions) -- the harness enforces it
against the real system of record before any tool runs.

```
precondition (vs real state) -> tool allowlist -> approval gate ->
execute via adapter -> postcondition (vs real state) -> checkpoint + trace -> next step
```

If a precondition fails, the step **blocks before any tool runs**.
If a postcondition fails, the outcome is **rejected** even when the agent claims success.

## Quick start

```bash
pip install -e .
rh run examples/vendor_onboarding.yaml --case examples/cases/missing_tax_id.json
```

The case is missing a required document. The harness blocks `create_vendor_draft` before
it runs -- prevention, not detection:

```
  ✓ intake_validate

 BLOCK  prepare_erp_draft
   precondition failed: docs.contains_all(["w9", "bank_letter"])
   reason: required_docs_present=false
   blocked tool: create_vendor_draft
   trace: runs/vendor_onboarding/episode_001.jsonl
```

Fix the data and the step passes:

```bash
rh run examples/vendor_onboarding.yaml --case examples/cases/complete.json
```

```
  ✓ intake_validate
  ✓ prepare_erp_draft
```

## What it does

Every runbook step runs the same enforced loop:

1. **Preconditions** -- predicates evaluated against the system of record (ERP, CRM, DB, APIs).
   If they fail, the step blocks. No tool runs.
2. **Tool allowlist** -- only the declared tools are callable. Everything else is refused.
3. **Approval gate** -- human sign-off for risky actions (CLI, webhook, queue).
4. **Execute** -- the agent acts, confined to the allowed tools.
5. **Postconditions** -- verified against the system of record, not the agent's output.
   If they fail, the outcome is rejected.
6. **Checkpoint + trace** -- state hash + OTEL spans for replay and observability.

The control flow is deterministic Python. The LLM only acts inside step 4.

## What it doesn't do

- **Not observability.** Laufwise is not a tracer, dashboard, or annotation queue.
  It's the enforcement layer underneath your observability stack.
- **Not an agent framework.** It wraps any agent (LangGraph, raw LLM, MCP) via the
  `ExecutionAdapter` protocol. Bring your own agent.
- **Not evals.** Evals assert on outputs after the fact. Laufwise asserts on world state
  and blocks before the action runs.

## Feeds your stack

Laufwise emits [OpenTelemetry](https://opentelemetry.io/) spans with `gen_ai.*` semantic
conventions. Any OTLP-compatible backend consumes them:

```
Laufwise harness
    |
    |  OTEL spans (gen_ai.*)
    v
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  Langfuse    │  │  LangSmith  │  │   Grafana   │
│  /api/otel   │  │  OTLP       │  │   Tempo     │
└─────────────┘  └─────────────┘  └─────────────┘
```

Install the optional OTEL dependencies:

```bash
pip install -e ".[otel]"
```

Configure via environment variables:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="https://cloud.langfuse.com/api/public/otel"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <your-key>"
```

Feeds your LangSmith/Langfuse via OTEL. Doesn't replace it.

## How it stays horizontal

The engine knows nothing about vendors -- or any domain. `examples/vendor_onboarding.yaml` is
**data**. Point the same harness at a different runbook + state fixture and it runs any
process, unchanged. That's the test of a primitive.

## Concepts

- **Runbook** -- an ordered process contract (`examples/*.yaml`). Each step declares its
  preconditions, tool allowlist, approval policy, execution, and postconditions.
- **Check** -- a pure predicate over state (`docs.contains_all([...])`, `vendor.exists ==
  false`). Evaluated against the system of record, never against model text.
- **StateProvider** -- the system of record. v0 ships `MemoryStateProvider` (a JSON fixture);
  ERP/CRM/DB/MCP providers come later.
- **Seams** -- `Engine`, `StateProvider`, `ExecutionAdapter`, `DurableStore`, `TraceSink`,
  `ApprovalGate`, `CheckEvaluator`. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Status

v0 -- the first-run demo. Implemented: spec loader, check DSL, memory state provider, local
engine, JSONL trace, OTEL trace sink, CLI. Stubbed: approval UI, execution adapters,
`rh test` / `rh replay`, SQLite durable store, Temporal engine. Roadmap in
[ARCHITECTURE.md](ARCHITECTURE.md).

```bash
pytest tests/ -v   # run the suite
```

## License

MIT
