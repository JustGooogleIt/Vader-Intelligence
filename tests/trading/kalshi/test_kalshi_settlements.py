"""Live settlement recording against the SYNTHETIC exchange double (throwaway keys, temp
dirs; nothing is sent anywhere): a venue settlement is recorded only when it agrees
exactly with the ledger's holding, payout and orders; anything else is reported and
blocks risk-increasing orders until a later reconciliation agrees."""

from dataclasses import replace

import pytest
from _trading_synthetic import OTHER, TICKER, D

from vader_intelligence.trading.executor import SETTLEMENTS_CURSOR
from vader_intelligence.trading.kalshi.mapping import MappingError, settlement_from_exchange

from ._kalshi_double import Harness
from .test_kalshi_gateway import place


def holding_one_fill(tmp_path, *, hidden=False):
    h = Harness(tmp_path / "l.sqlite3")
    buy = place(h, 10, tif="gtc")
    held = buy.order.quantity
    h.double.execute(buy.submit.exchange_order_id, held, fee="0.02", hidden=hidden)
    return h, held


def test_a_settlement_that_matches_the_holding_is_recorded_once(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    cash_before = h.ledger.projection().portfolio.cash
    h.at(3600)
    h.double.settle(TICKER, "yes")
    report = h.executor.reconcile(now=h.at(3700))
    assert report.clean, report.unresolved
    (settlement,) = h.ledger.settlements()
    assert settlement.settlement_id == "kalshi:" + TICKER
    assert (settlement.result, settlement.settlement_value) == ("yes", D(1))
    portfolio = h.ledger.projection().portfolio
    assert portfolio.cash == cash_before + held == h.double.cash
    assert TICKER not in portfolio.holdings
    assert h.ledger.processed(SETTLEMENTS_CURSOR)["cursor"].startswith("ts:")
    # Re-reads (overlap, a restart with fresh gateway memory) record nothing new.
    assert h.executor.reconcile(now=h.at(3800)).clean
    h.restart()
    assert h.executor.reconcile(now=h.at(3900)).clean
    assert len(h.ledger.settlements()) == 1
    assert h.ledger.verify() == []


def test_a_no_result_pays_nothing_on_a_yes_holding(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    cash_before = h.ledger.projection().portfolio.cash
    h.double.settle(TICKER, "no")
    assert h.executor.reconcile(now=h.at(3700)).clean
    portfolio = h.ledger.projection().portfolio
    assert portfolio.cash == cash_before == h.double.cash
    assert TICKER not in portfolio.holdings


def test_a_settlement_ahead_of_a_lagging_fill_waits_for_the_fill(tmp_path):
    h, held = holding_one_fill(tmp_path, hidden=True)  # REST has not served the fill yet
    cursor = h.ledger.processed(SETTLEMENTS_CURSOR)  # from the reconciliation before placing
    h.double.settle(TICKER, "yes")
    report = h.executor.reconcile(now=h.at(3700))
    # The venue's order is executed but the ledger's is not: still active.
    assert "settlement_orders_active:" + TICKER in report.unresolved
    assert any(r.startswith("fills_missing:") for r in report.unresolved)
    assert h.ledger.settlements() == []
    assert h.ledger.processed(SETTLEMENTS_CURSOR) == cursor  # read again next time
    h.double.publish()
    report = h.executor.reconcile(now=h.at(3800))
    assert report.clean, report.unresolved
    assert len(h.ledger.settlements()) == 1
    assert h.ledger.projection().portfolio.cash == h.double.cash


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"fee_cost": "0.0100"}, "settlement_fee"),
        ({"yes_count_fp": "1.00"}, "settlement_mismatch"),
        ({"revenue": 1}, "settlement_mismatch"),
    ],
)
def test_settlements_that_disagree_with_the_ledger_are_not_recorded(tmp_path, overrides, reason):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(TICKER, "yes", **overrides)
    report = h.executor.reconcile(now=h.at(3700))
    assert f"{reason}:{TICKER}" in report.unresolved
    assert h.ledger.settlements() == []
    assert h.executor.needs_reconcile


def test_foreign_conflicting_and_unreadable_settlements_fail_closed(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(OTHER, "yes")  # a market this ledger never traded
    assert "settlement_foreign:" + OTHER in h.executor.reconcile(now=h.at(30)).unresolved
    h.double.settlement_log.clear()
    h.double.settle(TICKER, "yes")
    assert h.executor.reconcile(now=h.at(3700)).clean
    # The venue later reports another settlement of the same market.
    h.double.settlement_log.append(h.double.settlement_log[-1] | {"market_result": "no"})
    assert "settlements_unavailable" in h.executor.reconcile(now=h.at(3800)).unresolved
    h.double.settlement_log[:] = [h.double.settlement_log[-1]]
    assert "settlement_conflict:" + TICKER in h.executor.reconcile(now=h.at(3900)).unresolved
    h.double.settlement_log[-1]["market_result"] = "void"
    assert "settlements_unavailable" in h.executor.reconcile(now=h.at(4000)).unresolved
    assert len(h.ledger.settlements()) == 1


def test_no_settlement_while_a_ledger_order_on_the_market_is_unresolved(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    resting = place(h, 30, tif="gtc", rationale="second")
    h.double.settle(TICKER, "yes")  # the venue cancels it at close
    h.double.inject("GET", r".*/portfolio/orders.*", 500, times=100)  # lookups fail
    report = h.executor.reconcile(now=h.at(3700))
    assert f"lookup_failed:{resting.order.client_order_id}" in report.unresolved
    assert "settlement_orders_active:" + TICKER in report.unresolved
    assert h.ledger.settlements() == []
    h.double.failures.clear()
    report = h.executor.reconcile(now=h.at(3800))
    assert report.clean, report.unresolved
    assert len(h.ledger.settlements()) == 1


def test_a_fill_after_the_recorded_settlement_stays_refused(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    buy = place(h, 10, tif="gtc")
    assert buy.order.quantity > 1
    h.double.execute(buy.submit.exchange_order_id, "1", fee="0.01")
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(TICKER, "yes")  # cancels the rest of the order
    assert h.executor.reconcile(now=h.at(3700)).clean
    (fill,) = [f for f in h.ledger.fills() if f.ticker == TICKER]
    late = replace(fill, fill_id=fill.fill_id + "-late")
    with pytest.raises(Exception, match="settled market"):
        h.executor.ingest([late], now=h.at(3800))
    assert "ingest_conflict" in h.executor.unknown_reasons


def record(**fields):
    base = {
        "ticker": TICKER,
        "market_result": "yes",
        "yes_count_fp": "2.00",
        "no_count_fp": "0.00",
        "revenue": 200,
        "settled_time": "2026-10-20T05:00:00Z",
        "fee_cost": "0.0000",
        "value": None,
    }
    return base | fields


def test_settlement_mapping():
    yes = settlement_from_exchange(record())
    assert (yes.result, yes.settlement_value, yes.revenue, yes.yes_count) == (
        "yes",
        D(1),
        D(2),
        D(2),
    )
    scalar = settlement_from_exchange(record(market_result="scalar", value=37, revenue=74))
    assert (scalar.result, scalar.settlement_value) == ("fair", D("0.37"))
    for bad in (
        record(market_result="void"),
        record(market_result="scalar"),  # no value
        record(market_result="yes", value=0),  # contradicts the result
        record(value=True),
        record(revenue="200"),
        record(revenue=-1),
        record(yes_count_fp="1.005"),
        record(settled_time="yesterday"),
        {k: v for k, v in record().items() if k != "fee_cost"},
    ):
        with pytest.raises(MappingError):
            settlement_from_exchange(bad)
