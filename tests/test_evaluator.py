import pytest

from laufwise.contract.evaluator import (
    BuiltinEvaluator,
    CheckEvalError,
    CheckSyntaxError,
)
from laufwise.state.base import StateView

EV = BuiltinEvaluator()


def _state():
    return {
        "vendor": StateView(None),
        "docs": StateView(["w9"]),
        "duplicates": StateView([]),
        "vendor_draft": StateView({"status": "draft", "note": "safe and sound"}),
    }


def test_field_bool_compare():
    assert EV.evaluate("vendor.exists == false", _state()).ok is True


def test_count_compare():
    s = _state()
    assert EV.evaluate("docs.count >= 1", s).ok is True
    assert EV.evaluate("docs.count >= 2", s).ok is False
    assert EV.evaluate("duplicates.count == 0", s).ok is True


def test_contains_all():
    s = _state()
    assert EV.evaluate('docs.contains_all(["w9"])', s).ok is True
    assert EV.evaluate('docs.contains_all(["w9", "bank_letter"])', s).ok is False


def test_field_string_compare():
    assert EV.evaluate('vendor_draft.status == "draft"', _state()).ok is True


def test_py_escape_hatch_refused():
    with pytest.raises(NotImplementedError):
        EV.evaluate("py:custom.predicate", _state())


# --- totality: the DSL must refuse what it cannot represent -------------------
# Regression for the false pass: the old right-hand side pattern swallowed the rest of the
# expression as a bare string, so `status != "approved" and docs.count >= 1` compared
# 'draft' against the literal text '"approved" and docs.count >= 1', found them unequal,
# and PASSED a check whose truth value is False. A check that can pass when it should fail
# is worse than no check, so an unrepresentable expression must raise, never evaluate.


@pytest.mark.parametrize(
    "expr",
    [
        'vendor_draft.status != "approved" and docs.count >= 1',  # the false pass
        'vendor_draft.status == "draft" and docs.count >= 1',
        "docs.count >= 1 or duplicates.count == 0",
        "not vendor.exists",
        "docs.count >= 1 && duplicates.count == 0",
        "docs.count >= 1 || duplicates.count == 0",
        "docs.count >= 1 AND duplicates.count == 0",  # case-insensitive
    ],
)
def test_boolean_connectives_are_refused_not_reinterpreted(expr):
    with pytest.raises(CheckSyntaxError, match="connective"):
        EV.evaluate(expr, _state())


def test_connective_words_inside_string_literals_stay_legal():
    # `and` here is ordinary text, not an operator — masking quoted spans must keep this working.
    assert EV.evaluate('vendor_draft.note == "safe and sound"', _state()).ok is True


@pytest.mark.parametrize(
    "expr",
    [
        "vendor_draft.status == draft",  # bare word: quote it or it is not a value
        'vendor_draft.status == "draft',  # unterminated quote
        'vendor_draft.status == "draft" extra',  # trailing content
        "docs.contains_all([)",  # malformed JSON argument
        "docs.count >=",  # operator with no value
        "docs.count 5",  # missing operator
        "docs",  # not a binding.member expression
    ],
)
def test_malformed_expressions_raise_syntax_error(expr):
    with pytest.raises(CheckSyntaxError):
        EV.evaluate(expr, _state())


def test_unknown_binding_is_a_syntax_error():
    with pytest.raises(CheckSyntaxError, match="unknown state binding"):
        EV.evaluate('vendr.status == "draft"', _state())


def test_incomparable_values_raise_eval_error_not_typeerror():
    # Well formed, but this state cannot satisfy it: dynamic, so it surfaces as CheckEvalError
    # (the engine turns that into a traced ruling instead of crashing mid-run).
    with pytest.raises(CheckEvalError):
        EV.evaluate('docs.count > "abc"', _state())


# --- validate(): the same faults, caught before a run starts ------------------


def test_validate_accepts_a_well_formed_expression():
    EV.validate('vendor.status == "draft"', {"vendor"})


def test_validate_rejects_undeclared_binding():
    with pytest.raises(CheckSyntaxError, match="undeclared state binding"):
        EV.validate('vendr.status == "draft"', {"vendor"})


def test_validate_rejects_connectives_and_bad_literals():
    with pytest.raises(CheckSyntaxError):
        EV.validate("docs.count >= 1 and vendor.exists == true", {"docs", "vendor"})
    with pytest.raises(CheckSyntaxError):
        EV.validate("vendor.status == draft", {"vendor"})


def test_validate_without_bindings_checks_syntax_only():
    EV.validate("anything.goes == 1")