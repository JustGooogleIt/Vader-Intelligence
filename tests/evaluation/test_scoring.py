"""Synthetic hand-calculated probabilities and labels; no archive or live evidence."""

from decimal import ROUND_DOWN, Decimal, DefaultContext, Inexact, localcontext

import pytest

from vader_intelligence.evaluation.scoring import (
    brier,
    clipped_probability,
    log_loss,
    probability,
)


@pytest.mark.parametrize(
    "p,y,expected",
    [
        ("0.5", 0, "0.25"),
        ("0.5", 1, "0.25"),
        ("0.4101", 1, "0.34798201"),
        ("0.4101", 0, "0.16818201"),
        ("0", 0, "0"),
        ("1", 1, "0"),
        ("0", 1, "1"),
        ("1", 0, "1"),
    ],
)
def test_hand_calculated_brier(p, y, expected):
    assert brier(p, y) == Decimal(expected)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        0,
        1,
        0.5,
        float("nan"),
        float("inf"),
        "NaN",
        "Infinity",
        "-0.1",
        "-0",
        "+0.5",
        "1.01",
        "1e-2",
        " .5",
        ".5",
        "0.5 ",
        "０.５",
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-0"),
        Decimal("1E-999999999"),
        "0." + "1" * 39,
    ],
)
def test_invalid_probabilities_rejected_everywhere(bad):
    for function in (probability, clipped_probability):
        with pytest.raises(ValueError):
            function(bad)
    for function in (brier, log_loss):
        with pytest.raises(ValueError):
            function(bad, 1)


@pytest.mark.parametrize("bad", [True, False, "yes", "0", Decimal("1"), 1.0, -1, 2, None])
def test_labels_must_be_explicit_binary_integers(bad):
    for function in (brier, log_loss):
        with pytest.raises(ValueError):
            function("0.5", bad)


@pytest.mark.parametrize(
    "p,q",
    [
        ("0", "0.000001"),
        ("0.0000009", "0.000001"),
        ("0.000001", "0.000001"),
        ("0.0000011", "0.0000011"),
        ("0.9999989", "0.9999989"),
        ("0.999999", "0.999999"),
        ("0.9999991", "0.999999"),
        ("1", "0.999999"),
    ],
)
def test_clipping_boundaries_preserve_probability(p, q):
    original = probability(p)
    assert clipped_probability(original) == Decimal(q)
    assert original == Decimal(p)
    assert brier(original, 0) == brier(p, 0)


def test_log_golden_values_and_complements():
    assert abs(
        log_loss("0.5", 1) - Decimal("0.6931471805599453094172321214581765680755")
    ) < Decimal("1e-38")
    assert abs(log_loss("0", 1) - Decimal("13.81551055796427410410794872810618524561")) < Decimal(
        "1e-37"
    )
    assert log_loss("0", 1) == log_loss("1", 0)
    assert log_loss("0.4101", 1) == log_loss("0.5899", 0)


def test_ambient_decimal_context_is_not_an_input():
    expected = (brier("0.4101", 1), log_loss("0.4101", 1), clipped_probability("1"))
    with localcontext() as context:
        context.prec, context.rounding = 2, ROUND_DOWN
        context.traps[Inexact] = True
        assert (brier("0.4101", 1), log_loss("0.4101", 1), clipped_probability("1")) == expected
        assert context.prec == 2 and context.traps[Inexact]


def test_global_default_context_cannot_change_scores(monkeypatch):
    expected = log_loss("0.4101", 1)
    monkeypatch.setitem(DefaultContext.traps, Inexact, True)
    assert log_loss("0.4101", 1) == expected
