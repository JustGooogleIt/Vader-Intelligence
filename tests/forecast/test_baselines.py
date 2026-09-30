"""Synthetic book fixtures; arithmetic acceptance does not establish provenance."""

from copy import deepcopy
from decimal import Decimal, Inexact, localcontext

import pytest

from vader_intelligence.forecast.baselines import InvalidBook, constant, midpoint


def book(yes=None, no=None):
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.4001", "2.00"]] if yes is None else yes,
            "no_dollars": [["0.5799", "3.00"]] if no is None else no,
        }
    }


def test_constant_and_same_book_midpoint():
    value = book()
    original = deepcopy(value)
    assert constant() == Decimal("0.5")
    assert midpoint(value) == Decimal("0.4101")
    assert value == original
    assert midpoint(book([["0.4000", "1"]], [["0.5999", "1"]])) == Decimal("0.40005")


def test_locked_endpoints_and_zero_quantity_levels():
    for yes, no, expected in [("0", "1", "0"), ("1", "0", "1"), ("0.4", "0.6", "0.4")]:
        assert midpoint(book([[yes, "1"]], [[no, "1"]])) == Decimal(expected)
    assert midpoint(book([["0.4001", "1"], ["0.99", "0"]])) == Decimal("0.4101")


@pytest.mark.parametrize(
    "bad",
    [
        [["0.4", "1"], ["0.40", "2"]],
        [["0.3", "1"], ["0.2", "1"]],
        [["0.4", "0"]],
        [["0.4", "-1"]],
        [["0.4", "NaN"]],
        [["0.4", "1.001"]],
        [[0.4, "1"]],
        [["0.40001", "1"]],
        [["1.01", "1"]],
        [["4e-1", "1"]],
        [["Infinity", "1"]],
        [["0.4"]],
        "invalid",
    ],
)
def test_invalid_side_rejected(bad):
    with pytest.raises(InvalidBook):
        midpoint(book(yes=bad))


@pytest.mark.parametrize(
    "state,reason",
    [("missing", "book_side_missing"), (None, "book_side_null"), ([], "book_side_empty")],
)
def test_side_states_are_distinct(state, reason):
    value = book()
    if state == "missing":
        del value["orderbook_fp"]["no_dollars"]
    else:
        value["orderbook_fp"]["no_dollars"] = state
    with pytest.raises(InvalidBook) as exc:
        midpoint(value)
    assert exc.value.reason == reason


def test_flags_crossing_missing_and_notional():
    with pytest.raises(InvalidBook) as exc:
        midpoint(book([["0.3", "1"], ["0.2", "1"]]))
    assert exc.value.reason == "book_flagged" and exc.value.flags == ("yes_unsorted",)
    with pytest.raises(InvalidBook, match="book_crossed"):
        midpoint(book([["0.9", "1"]]))
    with pytest.raises(InvalidBook, match="book_missing"):
        midpoint(None)
    with pytest.raises(InvalidBook, match="unsupported_notional"):
        midpoint(book(), "2")


def test_arithmetic_does_not_depend_on_callers_context():
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert midpoint(book()) == Decimal("0.4101")
