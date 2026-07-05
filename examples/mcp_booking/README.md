# The MCP step session: wrap any MCP server in a process contract

The flagship demo. Three processes: an agent (MCP client) → `rh serve` (the contract) →
a calendar MCP server (the tools) → a real HTTP API (the system of record). The agent's
claim is identical in both runs — the *verdict* differs, because laufwise re-queries the
calendar instead of trusting the claim.

```bash
pip install -e ".[mcp]"

# terminal 1: the system of record (mock calendar REST API)
python examples/mcp_booking/api_server.py 8975

# terminal 2: drive the session like an agent would
python examples/mcp_booking/drive_session.py --lie   # downstream claims success, writes nothing
python examples/mcp_booking/drive_session.py         # downstream actually writes
```

Lying downstream:

```
tools before begin_step: ['begin_step', 'complete_step', 'runbook_status']
begin_step -> step_active, allowed=['create_event']
tools while active:      [..., 'create_event', ...]
agent's claim:           You're all set — 'Interview c-1' booked for 2026-07-08T14:00:00!
complete_step ->         {"status": "reject", "reason": "no calendar event found — booking claim not verified"}
```

Honest downstream: same claim, `complete_step -> {"status": "ok", "runbook": "complete"}`.

What the session enforces (ARCHITECTURE §1.6):

- **Scoped visibility**: before `begin_step`, only the control tools exist. While a step is
  active, only that step's allowlisted tools are visible and forwarded — `delete_event`
  exists on the calendar server, but the contract never exposes it.
- **The agent requests, the engine rules**: `begin_step` checks preconditions against the
  real API (slot actually free?) and the approval gate; `complete_step` re-queries the
  system of record with bounded re-verification (`verify: retries`). No step boundary is
  ever inferred from traffic.
- **Halts bind**: after a REJECT, `begin_step` refuses — step N+1 is unreachable until the
  process owner intervenes. Every ruling and refusal lands in the episode trace.

Point your own client at it the same way:

```bash
rh serve examples/mcp_booking/booking.yaml \
  --case examples/mcp_booking/case.json \
  --wrap "python examples/mcp_booking/calendar_mcp.py http://127.0.0.1:8975"
```