"""The check evaluator — the make-or-break component.

A check is a PURE function of state (CLAUDE.md invariant #2). The v0 BuiltinEvaluator is a
deliberately tiny DSL: enough for the demo, no more. It is hidden behind the CheckEvaluator
protocol so CEL (cel-python) can drop in later without touching the engine.

Supported forms:
    binding.field  op  value        vendor.exists == false   vendor.status == "draft"
    binding.count  op  value        docs.count >= 2          duplicates.count == 0
    binding.method(args)            docs.contains_all(["w9", "bank_letter"])

The `py:` escape hatch is detected and refused in v0 — leaving the sandbox must be a conscious
decision, never the default path.
"""

from __future__ import annotations

import ast
import json
import operator
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from laufwise.state.base import StateView

_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}

# binding . member ( args )?   ( op rhs )?
_PATTERN = re.compile(
    r"^\s*(\w+)\.(\w+)\s*(?:\((.*)\))?\s*(?:(==|!=|>=|<=|>|<)\s*(.+))?\s*$"
)


@dataclass
class CheckResult:
    ok: bool
    detail: str


@runtime_checkable
class CheckEvaluator(Protocol):
    def evaluate(self, expr: str, state: dict[str, StateView]) -> CheckResult: ...


def _parse_value(s: str) -> Any:
    s = s.strip()
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null" or low == "none":
        return None
    if s[:1] in "[{":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return ast.literal_eval(s)
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


class BuiltinEvaluator:
    """Tiny regex-based DSL evaluator. Pure: reads state, returns a verdict, no side effects."""

    def evaluate(self, expr: str, state: dict[str, StateView]) -> CheckResult:
        if expr.strip().startswith("py:"):
            raise NotImplementedError(
                "py: escape hatch is not implemented in v0 — keep checks declarative"
            )

        m = _PATTERN.match(expr)
        if not m:
            raise ValueError(f"cannot parse check expression: {expr!r}")

        binding, member, args, op, rhs = m.groups()

        if binding not in state:
            raise KeyError(f"unknown state binding {binding!r} in check {expr!r}")
        view = state[binding]

        # Resolve the left-hand value.
        if args is not None:
            method = getattr(view, member, None)
            if not callable(method):
                raise ValueError(f"{binding}.{member} is not a callable accessor")
            left = method(_parse_value(args))
        elif member == "exists":
            left = view.exists
        elif member == "count":
            left = view.count
        else:
            left = view.get_field(member)

        # No comparison operator => the left value must itself be truthy (method predicate).
        if op is None:
            ok = bool(left)
            return CheckResult(ok, f"{expr} -> {left!r}")

        want = _parse_value(rhs)
        ok = _OPS[op](left, want)
        return CheckResult(ok, f"{binding}.{member}={left!r} {op} {want!r} -> {ok}")