"""Synthetic, already-qualified candidates; no real game identity is asserted."""

from itertools import permutations

import pytest

from vader_intelligence.forecast.selection import select_contract


def candidate(event, suffix, team, **extra):
    return dict(
        event_ticker="KXMLBGAME-" + event,
        ticker="KXMLBGAME-" + event + "-" + suffix,
        yes_team_id=team,
        **extra,
    )


def test_selection_is_order_price_outcome_and_status_independent():
    values = [
        candidate("A", "LOW-Z", 111),
        candidate("A", "LOW-A", 111),
        candidate("A", "HIGH", 112),
        candidate("B", "LOW", 111),
        candidate("B", "HIGH", 112),
    ]
    expected = select_contract(values, (112, 111))
    assert expected.contract.ticker == "KXMLBGAME-A-LOW-A"
    assert len(expected.alternatives) == 4
    for order in permutations(values):
        assert select_contract(order, (111, 112)) == expected
    changed = [dict(v, price="0.99", outcome="yes", status="open") for v in values]
    changed[1].update(price=None, outcome="no", status="closed")
    assert select_contract(changed, (111, 112)) == expected


def test_no_fallback_when_selected_event_is_incomplete():
    values = [candidate("A", "HIGH", 112), candidate("B", "LOW", 111), candidate("B", "HIGH", 112)]
    assert select_contract(values, (111, 112)).reasons == ("selected_team_missing",)
    assert select_contract([candidate("A", "LOW", 111)], (111, 112)).reasons == (
        "event_team_set_mismatch",
    )
    assert select_contract([], (111, 112)).reasons == ("no_contract_known",)


def test_conflicting_team_and_identical_duplicate_inputs():
    values = [candidate("A", "LOW", 111), candidate("A", "HIGH", 112)]
    assert select_contract(values + values, (111, 112)) == select_contract(values, (111, 112))
    conflict = values + [candidate("A", "LOW", 112)]
    assert select_contract(conflict, (111, 112)).reasons == ("ambiguous_contract",)


@pytest.mark.parametrize(
    "values,teams",
    [
        ([], (111, 111)),
        ([], (True, 112)),
        ([], (111,)),
        ([], ("111", 112)),
        ([candidate("A", "LOW", 111)] * 201, (111, 112)),
        ([candidate("A", "LOW", True)], (111, 112)),
        ([{"event_ticker": "OTHER", "ticker": "OTHER", "yes_team_id": 111}], (111, 112)),
        ([{}], (111, 112)),
        ("bad", (111, 112)),
    ],
)
def test_unqualified_or_unbounded_inputs_rejected(values, teams):
    with pytest.raises(ValueError):
        select_contract(values, teams)
