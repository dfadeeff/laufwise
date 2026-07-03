# Booking verification against a real API (http StateProvider)

The failure demo: an agent claims "you're all set for Tuesday!" — laufwise re-queries the
calendar API and only accepts the outcome if the event actually exists.

```bash
# terminal 1: a mock calendar API (any JSON API works the same way)
python examples/http_booking/mock_calendar.py 8973

# terminal 2: the write never landed -> REJECT (after 2 bounded re-verifications)
rh run examples/http_booking/book_interview.yaml --case examples/http_booking/cases/broken_write.json

# the write landed -> OK
rh run examples/http_booking/book_interview.yaml --case examples/http_booking/cases/write_landed.json
```

What this shows:

- **Preconditions against real state**: `slot.exists == true` is resolved via HTTP before any
  tool may run.
- **Postconditions re-query the system of record**: the agent's claim is never sufficient
  (CLAUDE.md invariant #2). An empty calendar REJECTs the step, exit code 1, trace written.
- **`verify` absorbs read-after-write lag**: `retries: 2, backoff_s: 0.2` re-checks the same
  postconditions against freshly resolved state — re-verification, never re-execution.
- **Case-driven grounding**: the case fixture's `_params` template the binding URLs, so the
  same runbook runs against any deployment of the same API.