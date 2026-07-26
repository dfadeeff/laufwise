"""The check evaluator — the make-or-break component.

A check is a PURE function of state (CLAUDE.md invariant #2). The v0 BuiltinEvaluator is a
deliberately tiny DSL: enough for the demo, no more. It is hidden behind the CheckEvaluator
protocol so CEL (cel-python) can drop in later without touching the engine.

Supported forms:
    binding.field  op  value        vendor.exists == false   vendor.status == "draft"
    binding.count  op  value        docs.count >= 2          duplicates.count == 0
    binding.method(args)            docs.contains_all(["w9", "bank_letter"])

**The grammar is total: every expression is either fully modelled or rejected.** A tiny DSL
is only trustworthy if it refuses what it cannot represent. An evaluator that silently
reinterprets an expression it half-understands produces false passes, and a check that can
pass when it should fail is worse than no check at all — ARCHITECTURE §1.5 claims
"deterministic, no false passes", and this module has to earn it. So: boolean connectives are
refused by name, and a right-hand side that is not a complete literal is a syntax error,
never a bare-string fallback.

Faults are split by when they are detectable:
    CheckSyntaxError — static; malformed regardless of state, so it is caught at load
                       (see `BuiltinEvaluator.validate`), not mid-run.
    CheckEvalError   — dynamic; well formed, but this state cannot satisfy the operation
                       (e.g. ordering a string against an int).

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

# One complete quoted literal: "..." or '...', backslash escapes allowed.
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')

# Boolean connectives. The DSL models no composition, so these must be REFUSED rather than
# swallowed into a right-hand side — that swallowing is precisely the false-pass bug.
_CONNECTIVE = re.compile(r"\b(and|or|not)\b|&&|\|\|", re.IGNORECASE)


class CheckError(Exception):
    """Base for every fault raised by a CheckEvaluator."""


class CheckSyntaxError(CheckError, ValueError):
    """The expression is not valid in this DSL — statically detectable, state-independent."""


class CheckEvalError(CheckError):
    """The expression is well formed but cannot be evaluated against this state."""


@dataclass
class CheckResult:
    ok: bool
    detail: str


@runtime_checkable
class CheckEvaluator(Protocol):
    def evaluate(self, expr: str, state: dict[str, StateView]) -> CheckResult: ...


def _mask_strings(expr: str) -> str:
    """Blank out quoted literals so connective scanning cannot be fooled by ordinary text —
    `note == "safe and sound"` is a legal check and must stay one."""
    return _STRING.sub(lambda m: "\x00" * len(m.group(0)), expr)


def _reject_connectives(expr: str) -> None:
    found = _CONNECTIVE.search(_mask_strings(expr))
    if found is None:
        return
    raise CheckSyntaxError(
        f"check {expr!r} uses the boolean connective {found.group(0)!r}, which the built-in "
        "DSL does not support. Write one check per condition — a step's checks are ANDed, so "
        "`- check: a.x == 1` plus `- check: b.y >= 2` is the supported form. For or/not, "
        "invert the comparison (`x.field != value`) or swap in a CEL evaluator."
    )


def _parse_value(s: str, expr: str) -> Any:
    """Parse a COMPLETE literal. Anything else is a syntax error: there is deliberately no
    bare-word fallback, because falling back to a raw string is how a half-parsed expression
    becomes a silently wrong comparison."""
    s = s.strip()
    if not s:
        raise CheckSyntaxError(f"check {expr!r}: expected a value, found nothing")

    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None

    if s[0] in "[{":
        try:
            return json.loads(s)
        except json.JSONDecodeError as exc:
            raise CheckSyntaxError(
                f"check {expr!r}: {s!r} is not a valid JSON array/object ({exc.msg})"
            ) from exc

    if s[0] in "\"'":
        # Must be ONE complete quoted literal spanning the whole value. Trailing content
        # means the expression continues past what this DSL models — refuse it.
        match = _STRING.match(s)
        if match is None or match.end() != len(s):
            raise CheckSyntaxError(
                f"check {expr!r}: {s!r} is not a single complete quoted string — "
                "unterminated quote, or trailing content the DSL does not model"
            )
        try:
            value = json.loads(s) if s[0] == '"' else ast.literal_eval(s)
        except (json.JSONDecodeError, SyntaxError, ValueError) as exc:
            raise CheckSyntaxError(f"check {expr!r}: cannot read string literal {s!r}") from exc
        if not isinstance(value, str):
            raise CheckSyntaxError(f"check {expr!r}: {s!r} is not a string literal")
        return value

    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass

    raise CheckSyntaxError(
        f"check {expr!r}: cannot parse {s!r} as a value. The built-in DSL accepts "
        'true/false, null, numbers, "quoted strings", and JSON arrays/objects — '
        "bare words are not values, so quote them if a string was meant."
    )


class BuiltinEvaluator:
    """Tiny regex-based DSL evaluator. Pure: reads state, returns a verdict, no side effects.

    Total by construction: `parse` accepts only what the grammar fully models; everything
    else raises CheckSyntaxError instead of being partially interpreted.
    """

    def parse(self, expr: str) -> tuple[str, str, str | None, str | None, str | None]:
        """Statically parse `expr` into (binding, member, args, op, rhs), raising
        CheckSyntaxError unless it is a complete, fully modelled expression. State-free, so
        it doubles as the load-time validator."""
        if expr.strip().startswith("py:"):
            raise NotImplementedError(
                "py: escape hatch is not implemented in v0 — keep checks declarative"
            )

        _reject_connectives(expr)

        m = _PATTERN.match(expr)
        if not m:
            raise CheckSyntaxError(
                f"cannot parse check expression: {expr!r}. Supported forms: "
                "`binding.field op value`, `binding.count op value`, "
                "`binding.exists op value`, `binding.method(arg)`."
            )

        binding, member, args, op, rhs = m.groups()
        # Parse both literal positions now: a malformed value is a STATIC fault, so it must
        # surface at load time rather than at the moment a postcondition runs.
        if args is not None:
            _parse_value(args, expr)
        if op is not None and rhs is not None:  # the regex always pairs the two
            _parse_value(rhs, expr)
        return binding, member, args, op, rhs

    def validate(self, expr: str, bindings: set[str] | None = None) -> None:
        """Load-time check: the expression parses, and (when `bindings` is given) reads a
        state binding the runbook actually declares. Raises CheckSyntaxError on either."""
        binding = self.parse(expr)[0]
        if bindings is not None and binding not in bindings:
            raise CheckSyntaxError(
                f"check {expr!r} reads undeclared state binding {binding!r} "
                f"(declared: {sorted(bindings) or 'none'})"
            )

    def evaluate(self, expr: str, state: dict[str, StateView]) -> CheckResult:
        binding, member, args, op, rhs = self.parse(expr)

        if binding not in state:
            raise CheckSyntaxError(
                f"unknown state binding {binding!r} in check {expr!r} "
                f"(resolved bindings: {sorted(state) or 'none'})"
            )
        view = state[binding]

        # Resolve the left-hand value.
        if args is not None:
            method = getattr(view, member, None)
            if not callable(method):
                raise CheckSyntaxError(f"{binding}.{member} is not a callable accessor")
            left = method(_parse_value(args, expr))
        elif member == "exists":
            left = view.exists
        elif member == "count":
            left = view.count
        else:
            left = view.get_field(member)

        # No comparison operator => the left value must itself be truthy (method predicate).
        if op is None or rhs is None:
            ok = bool(left)
            return CheckResult(ok, f"{expr} -> {left!r}")

        want = _parse_value(rhs, expr)
        try:
            ok = _OPS[op](left, want)
        except TypeError as exc:
            # Well-formed expression, incomparable values (`docs.count > "abc"`, or a field
            # that resolved to None). Dynamic, so load-time validation cannot catch it — the
            # engine turns this into a traced ruling rather than a crash.
            raise CheckEvalError(
                f"check {expr!r}: cannot compare {left!r} {op} {want!r} ({exc})"
            ) from exc
        return CheckResult(ok, f"{binding}.{member}={left!r} {op} {want!r} -> {ok}")