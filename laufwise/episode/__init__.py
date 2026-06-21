"""Episode log (stub).

In a later phase a run becomes an append-only episode of
(step_id, state_hash, decision, tool_calls, outcome) so `rh replay` can re-drive the same
deterministic engine and assert identical control flow. v0 relies on the JSONL TraceSink as a
de-facto episode; this module is a placeholder for the dedicated replay format.
"""

from __future__ import annotations