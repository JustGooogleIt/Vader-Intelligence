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


# ------------------------------------------------------------------ F3.5 summaries


def test_score_pair_strings_and_clipping_flag():
    from vader_intelligence.evaluation.scoring import score_pair

    scores, clipped = score_pair("0.4101", 1)
    assert scores["brier"] == "0.34798201" and clipped is False
    assert scores["log_loss"].startswith("0.89135")  # -ln(0.4101)
    scores, clipped = score_pair("1", 0)
    assert scores["brier"] == "1" and clipped is True
    assert scores["log_loss"].startswith("13.8155105579")  # -ln(0.000001)
    scores, clipped = score_pair("0", 0)
    assert scores["brier"] == "0" and clipped is True
    assert scores["log_loss"].startswith("0.00000100000050")  # -ln(1 - 1e-6)
    with pytest.raises(ValueError):
        score_pair("1.5", 1)
    with pytest.raises(ValueError):
        score_pair("0.5", 2)


def test_mean_is_exact_and_null_for_empty():
    from vader_intelligence.evaluation.scoring import mean

    assert mean([]) is None
    assert mean(["0.25"]) == "0.25"
    assert mean(["0.25", "0.126025"]) == "0.1880125"
    assert mean(["1", "0", "0"]).startswith("0.3333333333333333333333333333333333333333")
    with pytest.raises(ValueError):
        mean(["abc"])


def test_paired_summary_hand_cases():
    from vader_intelligence.evaluation.scoring import paired_summary, score_pair

    empty = paired_summary([])
    assert empty["n"] == 0 and empty["mean_brier_delta"] is None
    only_constant = paired_summary([{"constant-v1": score_pair("0.5", 1)[0], "midpoint-v1": None}])
    assert only_constant["n"] == 0 and only_constant["mean_brier_constant"] is None
    items = [
        {"constant-v1": score_pair("0.5", 1)[0], "midpoint-v1": score_pair("0.4101", 1)[0]},
        {"constant-v1": score_pair("0.5", 0)[0], "midpoint-v1": score_pair("0.4101", 0)[0]},
    ]
    summary = paired_summary(items)
    assert summary["n"] == 2
    assert summary["mean_brier_constant"] == "0.25"
    assert summary["mean_brier_midpoint"] == "0.25808201"  # (0.34798201 + 0.16818201) / 2
    assert summary["mean_brier_delta"] == "0.00808201"  # positive: constant did better
    # log-loss means are exact means of the per-game values under the v1 context
    from vader_intelligence.evaluation.scoring import arithmetic

    with arithmetic():
        expected = (
            Decimal(items[0]["midpoint-v1"]["log_loss"])
            + Decimal(items[1]["midpoint-v1"]["log_loss"])
        ) / 2
    assert Decimal(summary["mean_log_loss_midpoint"]) == expected
