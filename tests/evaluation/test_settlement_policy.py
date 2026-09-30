"""Synthetic raw provider examples; this tests eligibility, not outcome joins."""

import pytest

from vader_intelligence.evaluation.settlement import finalized_binary


def market(**changes):
    return dict(
        {
            "status": "finalized",
            "market_type": "binary",
            "result": "yes",
            "settlement_value_dollars": "1.0000",
            "notional_value_dollars": "1.0000",
            "settlement_ts": "2026-10-01T23:00:00Z",
        },
        **changes,
    )


def test_consistent_binary_labels_and_existing_provisional_semantics():
    assert finalized_binary(market(is_provisional=True)).label == 1
    assert finalized_binary(market(result="no", settlement_value_dollars="0.0000")).label == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"status": status}
        for status in ("active", "closed", "determined", "disputed", "amended", "unknown")
    ]
    + [{"result": "scalar", "settlement_value_dollars": value} for value in ("0", "1", "0.53")]
    + [
        {"settlement_value_dollars": "0.1234"},
        {"result": "no"},
        {"notional_value_dollars": "2"},
        {"settlement_value_dollars": 1.0},
        {"settlement_value_dollars": "NaN"},
        {"market_type": "scalar"},
        {"settlement_ts": None},
        {"settlement_ts": "2026-10-01"},
        {"settlement_ts": "bad"},
        {"updated_time": "bad"},
        {"result": []},
        {"status": {}},
    ],
)
def test_nonfinal_exceptional_and_invalid_never_score(changes):
    result = finalized_binary(market(**changes))
    assert result.label is None and not result.eligible and result.reasons


def test_missing_and_corrections_are_independent_inputs():
    assert not finalized_binary(None).eligible
    assert not finalized_binary({}).eligible
    # No sticky previous label or scalar-to-binary conversion.
    values = [
        market(),
        market(result="no", settlement_value_dollars="0"),
        market(),
        market(result="scalar", settlement_value_dollars="1"),
    ]
    assert [finalized_binary(value).label for value in values] == [1, 0, 1, None]
