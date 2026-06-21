from laufwise.contract.evaluator import BuiltinEvaluator
from laufwise.state.base import StateView

EV = BuiltinEvaluator()


def _state():
    return {
        "vendor": StateView(None),
        "docs": StateView(["w9"]),
        "duplicates": StateView([]),
        "vendor_draft": StateView({"status": "draft"}),
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
    import pytest

    with pytest.raises(NotImplementedError):
        EV.evaluate("py:custom.predicate", _state())