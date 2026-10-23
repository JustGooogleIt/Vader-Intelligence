"""Live settlement recording against the SYNTHETIC exchange double (throwaway keys, temp
dirs; nothing is sent anywhere): a venue settlement is recorded only when it agrees
exactly with the ledger's orders, fills, holding and payout; anything else is reported,
blocks risk-increasing orders, and is read again until it agrees or an operator resolves
it through the ledger."""

from datetime import timedelta

import pytest
from _trading_synthetic import TICKER, D, at, book, make_intent, market

from vader_intelligence.trading.executor import (
    SETTLEMENTS_CURSOR,
    acknowledge_settlement,
    settlement_ack_key,
    settlement_evidence,
)
from vader_intelligence.trading.gateway import UnreadableSettlement, settlement_fingerprint
from vader_intelligence.trading.kalshi.mapping import MappingError, settlement_from_exchange
from vader_intelligence.trading.ledger import LedgerConflict
from vader_intelligence.trading.portfolio import CashEvent, Settlement
from vader_intelligence.trading.risk import Approval, ForecastStatus

from ._kalshi_double import Harness, iso
from .test_kalshi_gateway import place

ELSEWHERE_EVENT = "SYN-26OCT19CCCDDD"
ELSEWHERE = ELSEWHERE_EVENT + "-CCC"
VOID = "SYN-26OCT19EEEFFF-EEE"


def place_on(h, t, ticker, event, probability="0.55", **intent_options):
    """`place` for any synthetic market (same book shape)."""
    now = h.at(t)
    intent = make_intent(ticker=ticker, event=event, t=t, probability=probability, **intent_options)
    snapshot = book(ticker=ticker, t=t - 5)
    return h.executor.place(
        intent,
        now=now,
        market=market(ticker=ticker, event=event),
        snapshot=snapshot,
        forecast=ForecastStatus(ticker, D(probability), at(t - 5), "synthetic-model"),
        marks={ticker: snapshot},
        equity_history=((at(-30), D(1000)),),
    )


def holding_one_fill(tmp_path, *, hidden=False):
    h = Harness(tmp_path / "l.sqlite3")
    buy = place(h, 10, tif="gtc")
    held = buy.order.quantity
    h.double.execute(buy.submit.exchange_order_id, held, fee="0.02", hidden=hidden)
    return h, held


def settled_cleanly(h, t):
    report = h.executor.reconcile(now=h.at(t))
    assert report.clean, report.unresolved
    assert h.ledger.projection().portfolio.cash == h.double.cash
    assert h.ledger.verify() == []


def evidence(h, token):
    """The venue's current records for `token` as the operator inspects them before
    acknowledging: `{fingerprint: summary}`."""
    return settlement_evidence(h.gateway, h.ledger)[token]


def acknowledge(h, token, note, t):
    """The runbook's acknowledgement: the operator looks at the records, then
    acknowledges exactly those."""
    return acknowledge_settlement(
        h.ledger, token, actor="operator", note=note, evidence=evidence(h, token), at=h.at(t)
    )


def test_a_settlement_that_matches_the_holding_is_recorded_once(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    cash_before = h.ledger.projection().portfolio.cash
    h.at(3600)
    h.double.settle(TICKER, "yes")
    settled_cleanly(h, 3700)
    (settlement,) = h.ledger.settlements()
    assert settlement.settlement_id == "kalshi:" + TICKER
    assert (settlement.result, settlement.settlement_value) == ("yes", D(1))
    portfolio = h.ledger.projection().portfolio
    assert portfolio.cash == cash_before + held
    assert TICKER not in portfolio.holdings
    assert h.ledger.processed(SETTLEMENTS_CURSOR)["cursor"].startswith("ts:")
    # Re-reads (overlap, a restart with fresh gateway memory) record nothing new.
    settled_cleanly(h, 3800)
    h.restart()
    settled_cleanly(h, 3900)
    assert len(h.ledger.settlements()) == 1


@pytest.mark.parametrize(
    "result,value,paid",
    [("no", None, "0"), ("yes", 100, "1"), ("scalar", 37, "0.37")],
)
def test_results_pay_the_yes_holding(tmp_path, result, value, paid):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    cash_before = h.ledger.projection().portfolio.cash
    h.double.settle(TICKER, result, value=value)
    settled_cleanly(h, 3700)
    (settlement,) = h.ledger.settlements()
    assert settlement.result == {"scalar": "fair"}.get(result, result)
    assert h.ledger.projection().portfolio.cash == cash_before + held * D(paid)


def test_a_no_holding_is_paid_on_a_no_result(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    buy = place(h, 10, side="no", price="0.56", probability="0.3", tif="gtc")
    assert isinstance(buy.decision, Approval), buy.decision
    held = buy.order.quantity
    h.double.execute(buy.submit.exchange_order_id, held, fee="0.02")
    assert h.executor.reconcile(now=h.at(20)).clean
    assert h.ledger.projection().portfolio.holding(TICKER).no_qty == held
    cash_before = h.ledger.projection().portfolio.cash
    h.double.settle(TICKER, "no")
    settled_cleanly(h, 3700)
    assert h.ledger.projection().portfolio.cash == cash_before + held


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
    settled_cleanly(h, 3800)
    assert len(h.ledger.settlements()) == 1


def netted_partial_fills(tmp_path, *, hidden):
    """A YES holding, then a buy and a sell on the same market, each partially filled by
    the same amount, so the missing fills net to zero contracts; settling cancels the
    rest of both (terminal at the venue before their fills are served)."""
    h = Harness(tmp_path / "l.sqlite3")
    first = place(h, 10, qty="6", tif="gtc")
    h.double.execute(first.submit.exchange_order_id, first.order.quantity, fee="0.02")
    assert h.executor.reconcile(now=h.at(12)).clean
    buy = place(h, 20, qty="4", tif="gtc", rationale="buy")
    sell = place(h, 22, action="sell", price="0.50", qty="4", tif="gtc", rationale="sell")
    assert buy.submit.status == sell.submit.status == "accepted"
    k = min(buy.order.quantity, sell.order.quantity) - 1
    assert k >= 1
    h.double.execute(buy.submit.exchange_order_id, k, fee="0", hidden=hidden)
    h.double.execute(sell.submit.exchange_order_id, k, fee="0", hidden=hidden)
    h.double.settle(TICKER, "yes")
    return h


def test_fills_the_venue_reported_must_all_be_recorded_before_settling(tmp_path):
    h = netted_partial_fills(tmp_path, hidden=True)
    report = h.executor.reconcile(now=h.at(3700))
    # The settled counts equal the ledger's holding, but both orders are short of the
    # fill counts the venue reported when it canceled them.
    assert "settlement_fills_missing:" + TICKER in report.unresolved
    assert h.ledger.settlements() == []
    h.double.publish()
    settled_cleanly(h, 3800)
    assert len(h.ledger.settlements()) == 1
    settled_cleanly(h, 7200)


def test_no_settlement_while_fills_cannot_be_read(tmp_path):
    h = netted_partial_fills(tmp_path, hidden=False)
    h.double.inject("GET", r".*/portfolio/fills", 500, times=50)
    report = h.executor.reconcile(now=h.at(3700))
    assert {"fills_unavailable", "settlements_deferred"} <= set(report.unresolved)
    assert h.ledger.settlements() == []
    h.double.failures.clear()
    settled_cleanly(h, 3800)


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


def test_operator_resolutions_clear_foreign_unreadable_and_fee_records(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(ELSEWHERE, "yes")  # a market this ledger never traded
    h.double.settle(VOID, "void")  # a result we cannot map
    h.double.settle(TICKER, "yes", fee="0.01")  # an unmodelled settlement fee
    report = h.executor.reconcile(now=h.at(3700))
    assert {
        "settlement_foreign:" + ELSEWHERE,
        "settlement_unreadable:" + VOID,
        "settlement_fee:" + TICKER,
    } <= set(report.unresolved)
    cursor = h.ledger.processed(SETTLEMENTS_CURSOR)
    # The operator investigates, acknowledges the two records outside this ledger, and
    # records the fee-bearing settlement and its fee through the ledger.
    for token in (ELSEWHERE, VOID):
        acknowledge(h, token, "not ours", 3750)
    h.ledger.append_settlement(
        Settlement("operator:" + TICKER, TICKER, "yes", "1", "operator:investigated", at(3600))
    )
    h.ledger.append_cash_event(
        CashEvent("operator:settlement-fee", "adjustment", D("-0.01"), at(3750), "venue fee")
    )
    settled_cleanly(h, 3800)
    assert h.ledger.processed(SETTLEMENTS_CURSOR) != cursor  # moved past the records


def test_two_unparseable_records_of_one_token_are_distinguishable(tmp_path):
    """Internal review of 553fa99, low finding 2: the evidence listing names what each
    unreadable record says, bounded, so the operator can tell them apart before
    acknowledging."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    first = h.double.settle(VOID, "void")
    h.double.settlement_log.append(
        first
        | {"market_result": "cancelled", "settled_time": iso(h.clock.now() + timedelta(seconds=1))}
    )
    assert h.executor.reconcile(now=h.at(3700)).unresolved == ("settlement_unreadable:" + VOID,)
    seen = evidence(h, VOID)
    assert len(seen) == 2
    assert sorted(v["as_listed"]["market_result"] for v in seen.values()) == ["cancelled", "void"]
    fields = {"ticker", "market_result", "settled_time", "value", "revenue", "fee_cost"}
    for v in seen.values():
        assert v["error"] == "unknown_result" and v["as_listed"]["ticker"] == VOID
        assert set(v["as_listed"]) <= fields
    only_void = {fp: v for fp, v in seen.items() if v["as_listed"]["market_result"] == "void"}
    acknowledge_settlement(
        h.ledger, VOID, actor="operator", note="void only", evidence=only_void, at=h.at(3750)
    )
    assert h.executor.reconcile(now=h.at(3800)).unresolved == ("settlement_unreadable:" + VOID,)
    acknowledge(h, VOID, "both reviewed", 3850)
    settled_cleanly(h, 3900)


def test_an_acknowledgement_needs_a_reason_and_the_evidence_it_resolves(tmp_path, caplog):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(VOID, "void")
    unreadable = ("settlement_unreadable:" + VOID,)
    assert h.executor.reconcile(now=h.at(3700)).unresolved == unreadable
    seen = evidence(h, VOID)
    assert len(seen) == 1 and all(v["error"] == "unknown_result" for v in seen.values())
    for bad in ({"note": " "}, {"evidence": ()}, {"evidence": "vs1:abc"}, {"evidence": [" "]}):
        with pytest.raises(ValueError):
            acknowledge_settlement(
                h.ledger,
                VOID,
                actor="operator",
                at=h.at(3750),
                **{"note": "void", "evidence": seen, **bad},
            )
    # Legacy (unscoped) acknowledgements name no evidence and cover nothing, with or
    # without data: the record is reported again and the operator is warned.
    h.ledger.mark_processed(settlement_ack_key(VOID), None, h.at(3750))
    with caplog.at_level("WARNING", logger="vader_intelligence.trading.executor"):
        assert h.executor.reconcile(now=h.at(3800)).unresolved == unreadable
    assert any("names no evidence" in r.message and VOID in r.message for r in caplog.records)
    # The scoped acknowledgement has its own key and resolves exactly these records.
    moment = h.at(3850)
    scoped = dict(actor="operator", note="void record", evidence=seen, at=moment)
    assert acknowledge_settlement(h.ledger, VOID, **scoped)
    settled_cleanly(h, 3900)
    # Exact retry: a no-op. The same evidence under another note: refused, never merged.
    assert not acknowledge_settlement(h.ledger, VOID, **scoped)
    with pytest.raises(LedgerConflict):
        acknowledge_settlement(h.ledger, VOID, **{**scoped, "note": "another note"})
    h.restart()
    settled_cleanly(h, 3950)


def test_two_records_for_one_ticker_block_only_that_ticker(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    first = h.double.settle(ELSEWHERE, "yes")
    h.double.settlement_log.append(first | {"market_result": "no"})
    h.double.settle(TICKER, "yes")
    report = h.executor.reconcile(now=h.at(3700))
    assert report.unresolved == ("settlement_unreadable:" + ELSEWHERE,)
    assert len(h.ledger.settlements()) == 1  # ours is recorded
    acknowledge(h, ELSEWHERE, "two records", 3750)
    settled_cleanly(h, 3800)


def test_an_unreadable_record_does_not_hide_this_ledgers_settlement(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(VOID, "void")
    h.double.settle(TICKER, "yes")
    report = h.executor.reconcile(now=h.at(3700))
    assert report.unresolved == ("settlement_unreadable:" + VOID,)
    assert len(h.ledger.settlements()) == 1
    assert h.ledger.projection().portfolio.cash == h.double.cash


def test_a_resettled_market_conflicts_until_the_operator_corrects_it(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(TICKER, "yes")
    settled_cleanly(h, 3700)
    # The venue later reports another result for the same market.
    h.double.settlement_log[-1] |= {"market_result": "no"}
    h.double.cash -= held
    report = h.executor.reconcile(now=h.at(3800))
    assert "settlement_conflict:" + TICKER in report.unresolved
    assert len(h.ledger.settlements()) == 1
    h.ledger.append_settlement(
        Settlement(
            "operator:" + TICKER + ":2",
            TICKER,
            "no",
            "0",
            "operator:venue resettled",
            at(3800),
            supersedes_id="kalshi:" + TICKER,
        )
    )
    settled_cleanly(h, 3900)


@pytest.mark.parametrize("listed", ["both records", "one record"])
def test_an_acknowledged_ticker_still_conflicts_when_the_venue_changes_its_result(tmp_path, listed):
    """Review N-34a: acknowledging this ledger's own ticker (two different records for it,
    resolved by the operator) must not hide a later change of the venue's result."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    first = h.double.settle(TICKER, "yes")
    later = iso(h.clock.now() + timedelta(seconds=1))
    duplicate = first | {"market_result": "no", "settled_time": later}  # contradictory
    h.double.settlement_log.append(duplicate)
    report = h.executor.reconcile(now=h.at(3700))
    assert "settlement_unreadable:" + TICKER in report.unresolved
    # The runbook's resolution: acknowledge the ticker and settle the ledger.
    acknowledge(h, TICKER, "two records", 3750)
    h.ledger.append_settlement(
        Settlement("operator:" + TICKER, TICKER, "yes", "1", "operator:investigated", at(3600))
    )
    settled_cleanly(h, 3800)  # one of the venue's records agrees: the ambiguity is resolved
    # The venue later settles the market NO.
    first["market_result"] = "no"
    if listed == "one record":
        h.double.settlement_log.remove(duplicate)
    h.double.cash -= held
    report = h.executor.reconcile(now=h.at(3900))
    assert "settlement_conflict:" + TICKER in report.unresolved, report.unresolved
    assert h.executor.needs_reconcile
    h.ledger.append_settlement(
        Settlement(
            "operator:" + TICKER + ":2",
            TICKER,
            "no",
            "0",
            "operator:venue resettled",
            at(3900),
            supersedes_id="operator:" + TICKER,
        )
    )
    if listed == "both records":
        # The venue still lists two records; the changed one was never acknowledged, so
        # the correction alone leaves it reported until the operator resolves it too.
        assert h.executor.reconcile(now=h.at(3950)).unresolved == (
            "settlement_unreadable:" + TICKER,
        )
        acknowledge(h, TICKER, "venue resettled", 3960)
    settled_cleanly(h, 4000)


def test_an_acknowledged_foreign_or_unreadable_record_stays_skipped(tmp_path):
    """Records of markets the ledger never settled stay skipped once acknowledged."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    foreign = h.double.settle(ELSEWHERE, "yes")
    h.double.settle(VOID, "void")
    h.double.settle(TICKER, "yes")
    assert not h.executor.reconcile(now=h.at(3700)).clean
    for token in (ELSEWHERE, VOID):
        acknowledge(h, token, "not ours", 3750)
    settled_cleanly(h, 3800)
    # The foreign record's content changes under the same source time: that is a record
    # the operator never saw, so it is reported again until acknowledged on its own.
    foreign["market_result"] = "no"
    report = h.executor.reconcile(now=h.at(3900))
    assert report.unresolved == ("settlement_foreign:" + ELSEWHERE,)
    h.restart()
    assert h.executor.reconcile(now=h.at(3950)).unresolved == ("settlement_foreign:" + ELSEWHERE,)
    acknowledge(h, ELSEWHERE, "changed, still not ours", 4000)
    settled_cleanly(h, 4050)


def test_another_spelling_of_the_settlement_time_is_the_same_record(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    record = h.double.settle(TICKER, "yes")
    settled_cleanly(h, 3700)
    record["settled_time"] = record["settled_time"].replace("Z", ".000+00:00")
    settled_cleanly(h, 3800)
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
    settled_cleanly(h, 3800)
    assert len(h.ledger.settlements()) == 1


def test_a_fill_listed_after_the_recorded_settlement_stays_refused(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    buy = place(h, 10, tif="gtc")
    assert buy.order.quantity > 1
    fill_id = h.double.execute(buy.submit.exchange_order_id, "1", fee="0.01")
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(TICKER, "yes")  # cancels the rest of the order
    settled_cleanly(h, 3700)
    cash = h.ledger.projection().portfolio.cash
    # The venue contradicts its own final fill count: one more fill of that order.
    late = dict(next(f for f in h.double.fill_log if f["fill_id"] == fill_id))
    late |= {"fill_id": "f-late-synthetic", "trade_id": "f-late-synthetic"}
    h.double.fill_log.append(late)
    report = h.executor.reconcile(now=h.at(3800))
    assert "fill_conflict:kalshi:f-late-synthetic" in report.unresolved
    assert h.ledger.projection().portfolio.cash == cash


def test_the_watermark_overlap_rereads_a_record_that_appears_late(tmp_path):
    h, held = holding_one_fill(tmp_path)
    other = place_on(h, 30, ELSEWHERE, ELSEWHERE_EVENT, tif="gtc", rationale="other")
    h.double.execute(other.submit.exchange_order_id, other.order.quantity, fee="0.02")
    assert h.executor.reconcile(now=h.at(40)).clean
    h.at(3600)
    h.double.settle(TICKER, "yes", hidden=True)  # settled now, listed only later
    h.at(5000)
    h.double.settle(ELSEWHERE, "yes")
    report = h.executor.reconcile(now=h.at(5100))
    assert report.unresolved == ("balance_mismatch", "position_mismatch:" + TICKER)
    assert h.ledger.processed(SETTLEMENTS_CURSOR)["cursor"] == f"ts:{epoch(at(5000))}"
    h.double.publish()  # 1400 s older than the watermark, inside the 3600 s overlap
    settled_cleanly(h, 5200)
    assert len(h.ledger.settlements()) == 2


def test_a_record_older_than_the_overlap_is_not_reread(tmp_path):
    h, held = holding_one_fill(tmp_path)
    other = place_on(h, 30, ELSEWHERE, ELSEWHERE_EVENT, tif="gtc", rationale="other")
    h.double.execute(other.submit.exchange_order_id, other.order.quantity, fee="0.02")
    assert h.executor.reconcile(now=h.at(40)).clean
    h.at(100)
    h.double.settle(TICKER, "yes", hidden=True)
    h.at(5000)
    h.double.settle(ELSEWHERE, "yes")
    assert not h.executor.reconcile(now=h.at(5100)).clean
    h.double.publish()  # 4900 s older than the watermark: outside the overlap (A22)
    report = h.executor.reconcile(now=h.at(5200))
    assert "position_mismatch:" + TICKER in report.unresolved  # visible, never silent
    assert len(h.ledger.settlements()) == 1


def test_the_cursor_never_passes_now(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    ahead = iso(h.clock.now() + timedelta(days=1))  # a venue clock a day ahead
    h.double.settle(TICKER, "yes", settled_time=ahead)
    settled_cleanly(h, 3700)
    assert h.ledger.processed(SETTLEMENTS_CURSOR)["cursor"] == f"ts:{epoch(at(3700))}"


def epoch(moment):
    return int(moment.timestamp())


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
    assert settlement_from_exchange(record(settled_time="2026-10-20T05:00:00.000+00:00")) == yes
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


def acknowledged_then_resettled(tmp_path):
    """N-34a scenario: two contradictory venue records, resolved by the operator (ack +
    operator settlement YES); the venue then settles both records NO."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    first = h.double.settle(TICKER, "yes")
    later = iso(h.clock.now() + timedelta(seconds=1))
    h.double.settlement_log.append(first | {"market_result": "no", "settled_time": later})
    assert "settlement_unreadable:" + TICKER in h.executor.reconcile(now=h.at(3700)).unresolved
    acknowledge(h, TICKER, "two records", 3750)
    h.ledger.append_settlement(
        Settlement("operator:" + TICKER, TICKER, "yes", "1", "operator:investigated", at(3600))
    )
    settled_cleanly(h, 3800)
    cursor = h.ledger.processed(SETTLEMENTS_CURSOR)
    first["market_result"] = "no"
    h.double.cash -= held
    return h, cursor


def test_an_acknowledged_conflict_persists_across_repeats_and_restarts_until_corrected(
    tmp_path,
):
    h, cursor = acknowledged_then_resettled(tmp_path)
    for t in (3900, 3950):  # repeat reads report the same conflict and keep the cursor
        report = h.executor.reconcile(now=h.at(t))
        assert "settlement_conflict:" + TICKER in report.unresolved
        assert h.executor.needs_reconcile
        assert h.ledger.processed(SETTLEMENTS_CURSOR) == cursor
    h.restart()  # a new executor with fresh gateway memory finds the same conflict
    assert "settlement_conflict:" + TICKER in h.executor.reconcile(now=h.at(4000)).unresolved
    assert len(h.ledger.settlements()) == 1
    h.ledger.append_settlement(
        Settlement(
            "operator:" + TICKER + ":2",
            TICKER,
            "no",
            "0",
            "operator:venue resettled",
            at(4000),
            supersedes_id="operator:" + TICKER,
        )
    )
    # Both listed records now agree with the ledger, but the changed one is uncovered.
    assert h.executor.reconcile(now=h.at(4050)).unresolved == ("settlement_unreadable:" + TICKER,)
    acknowledge(h, TICKER, "venue resettled", 4060)
    settled_cleanly(h, 4100)  # the watermark is unchanged: no newer settled_time appeared
    h.restart()
    settled_cleanly(h, 4200)
    assert len(h.ledger.settlements()) == 2


def resolved_pair(tmp_path):
    """Contradictory YES/$1 and NO/$0 records for the held market, acknowledged by the
    operator, who settles the ledger YES/$1 (the PR #39 review's starting point)."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    first = h.double.settle(TICKER, "yes")
    second = first | {
        "market_result": "no",
        "revenue": 0,
        "settled_time": iso(h.clock.now() + timedelta(seconds=1)),
    }
    h.double.settlement_log.append(second)
    assert "settlement_unreadable:" + TICKER in h.executor.reconcile(now=h.at(3700)).unresolved
    assert len(evidence(h, TICKER)) == 2
    acknowledge(h, TICKER, "resolve first pair", 3750)
    h.ledger.append_settlement(
        Settlement("operator:" + TICKER, TICKER, "yes", "1", "operator:first-pair", at(3600))
    )
    settled_cleanly(h, 3800)
    return h, first, held


@pytest.mark.parametrize("cash_changed", [False, True])
@pytest.mark.parametrize("result,payout_cents", [("no", 0), ("scalar", 50)])
def test_a_new_contradiction_is_not_covered_by_an_older_acknowledgement(
    tmp_path, cash_changed, result, payout_cents
):
    """PR #39 review P2: the old YES record still agrees with the ledger, but a genuinely
    later NO/$0 or scalar/$0.50 record was never resolved by the operator. It is a
    conflict whether or not the venue's balance already reflects it, on repeat reads and
    after a restart, until an immutable correction supersedes the settlement."""
    h, first, held = resolved_pair(tmp_path)
    cursor = h.ledger.processed(SETTLEMENTS_CURSOR)
    h.double.settlement_log.append(
        first
        | {
            "market_result": result,
            "value": payout_cents,
            "revenue": int(held * payout_cents),
            "settled_time": iso(h.clock.now() + timedelta(seconds=2)),
        }
    )
    if cash_changed:
        h.double.cash -= held * (100 - payout_cents) / 100
    conflict = "settlement_conflict:" + TICKER
    for t in (3900, 3950):
        report = h.executor.reconcile(now=h.at(t))
        assert conflict in report.unresolved, report.unresolved
        assert ("balance_mismatch" in report.unresolved) == cash_changed
        assert h.executor.needs_reconcile
        assert h.ledger.processed(SETTLEMENTS_CURSOR) == cursor
    h.restart()
    assert conflict in h.executor.reconcile(now=h.at(4000)).unresolved
    assert len(h.ledger.settlements()) == 1
    h.ledger.append_settlement(
        Settlement(
            "operator:" + TICKER + ":2",
            TICKER,
            "fair" if result == "scalar" else result,
            D(payout_cents) / 100,
            "operator:venue resettled",
            at(4000),
            supersedes_id="operator:" + TICKER,
        )
    )
    if not cash_changed:
        h.double.cash -= held * (100 - payout_cents) / 100  # the venue catches up
    # The correction is supported by the new record, which is still unacknowledged.
    assert h.executor.reconcile(now=h.at(4050)).unresolved == ("settlement_unreadable:" + TICKER,)
    acknowledge(h, TICKER, "venue resettled", 4060)
    settled_cleanly(h, 4100)
    h.restart()
    settled_cleanly(h, 4200)
    assert len(h.ledger.settlements()) == 2


@pytest.mark.parametrize("acknowledged", ["yes", "no"])
def test_a_partial_acknowledgement_never_records_the_other_record(tmp_path, acknowledged):
    """Internal review of f24af41, finding 1: acknowledging one record of a conflicting
    pair says nothing about the other; the executor must not record it."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    first = h.double.settle(TICKER, "yes")
    h.double.settlement_log.append(
        first
        | {
            "market_result": "no",
            "revenue": 0,
            "settled_time": iso(h.clock.now() + timedelta(seconds=1)),
        }
    )
    assert "settlement_unreadable:" + TICKER in h.executor.reconcile(now=h.at(3700)).unresolved
    one = {fp: v for fp, v in evidence(h, TICKER).items() if v["result"] == acknowledged}
    assert len(one) == 1
    acknowledge_settlement(
        h.ledger, TICKER, actor="operator", note="one of two", evidence=one, at=h.at(3750)
    )
    for t in (3800, 3850):
        report = h.executor.reconcile(now=h.at(t))
        assert "settlement_unreadable:" + TICKER in report.unresolved, report.unresolved
        assert h.ledger.settlements() == []
    h.restart()
    assert "settlement_unreadable:" + TICKER in h.executor.reconcile(now=h.at(3900)).unresolved
    assert h.ledger.settlements() == []
    # Resolution: acknowledge the other record too and settle the ledger.
    acknowledge(h, TICKER, "both reviewed", 3950)
    h.ledger.append_settlement(
        Settlement("operator:" + TICKER, TICKER, "yes", "1", "operator:investigated", at(3600))
    )
    settled_cleanly(h, 4000)
    assert [s.source_ref for s in h.ledger.settlements()] == ["operator:investigated"]


def test_an_acknowledged_record_still_supports_or_contradicts_the_ledgers_settlement(tmp_path):
    """Internal review finding 2: a covered record is not recorded, but the ledger's
    settlement must still be supported by a venue record. A mistyped operator settlement
    (NO while the only, acknowledged, record says YES) is a conflict; a correction that
    nothing the venue lists supports is a conflict too."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(TICKER, "yes", fee="0.01")
    assert "settlement_fee:" + TICKER in h.executor.reconcile(now=h.at(3700)).unresolved
    acknowledge(h, TICKER, "fee under review", 3750)
    h.ledger.append_settlement(
        Settlement("operator:" + TICKER, TICKER, "no", "0", "operator:typo", at(3600))
    )
    report = h.executor.reconcile(now=h.at(3800))
    assert "settlement_conflict:" + TICKER in report.unresolved, report.unresolved
    h.restart()
    assert "settlement_conflict:" + TICKER in h.executor.reconcile(now=h.at(3850)).unresolved
    h.ledger.append_settlement(
        Settlement(
            "operator:" + TICKER + ":2",
            TICKER,
            "yes",
            "1",
            "operator:corrected",
            at(3850),
            supersedes_id="operator:" + TICKER,
        )
    )
    h.ledger.append_cash_event(
        CashEvent("operator:settlement-fee", "adjustment", D("-0.01"), at(3850), "venue fee")
    )
    settled_cleanly(h, 3900)
    # The resolved pair: a correction to a result neither listed record carries conflicts.
    h2, first, held2 = resolved_pair(tmp_path / "pair")
    h2.ledger.append_settlement(
        Settlement(
            "operator:" + TICKER + ":2",
            TICKER,
            "fair",
            "0.5",
            "operator:unsupported",
            at(3850),
            supersedes_id="operator:" + TICKER,
        )
    )
    assert "settlement_conflict:" + TICKER in h2.executor.reconcile(now=h2.at(3900)).unresolved


def test_new_agreeing_records_in_a_group_are_reported_until_acknowledged(tmp_path):
    """Internal review finding 3: records the operator never acknowledged are reported
    even when they agree with the ledger on result and value (they differ in payout/fee)."""
    h, first, held = resolved_pair(tmp_path)
    later = h.clock.now() + timedelta(seconds=2)
    h.double.settlement_log.append(first | {"revenue": 0, "settled_time": iso(later)})
    h.double.settlement_log.append(
        first | {"fee_cost": "0.0500", "settled_time": iso(later + timedelta(seconds=1))}
    )
    for t in (3900, 3950):
        assert h.executor.reconcile(now=h.at(t)).unresolved == ("settlement_unreadable:" + TICKER,)
    h.restart()
    assert h.executor.reconcile(now=h.at(4000)).unresolved == ("settlement_unreadable:" + TICKER,)
    assert len(evidence(h, TICKER)) == 4
    acknowledge(h, TICKER, "re-listings reviewed", 4050)
    settled_cleanly(h, 4100)


def test_evidence_must_be_fingerprints_and_the_group_label_is_listing_order_free(tmp_path):
    """Internal review findings 5-7: the outer `settlement_evidence` mapping (ticker keys),
    mixed types and blanks are refused before any audit row is written; a group's error
    label does not depend on listing order."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(VOID, "void")
    assert not h.executor.reconcile(now=h.at(3700)).clean
    outer = settlement_evidence(h.gateway, h.ledger)
    for bad in (outer, ["vs1:" + "a" * 64, 1], [""], ["vs1:" + "a" * 63], ["raw1:" + "A" * 64]):
        with pytest.raises(ValueError):
            acknowledge_settlement(
                h.ledger, VOID, actor="operator", note="x", evidence=bad, at=h.at(3750)
            )
    assert not any(k.startswith("settlement_ack:") for k in h.ledger.processed_inputs())
    record = h.double.settle(TICKER, "yes")
    bad_value = record | {"value": 50}  # yes with a contradicting value: invalid_field
    bad_result = record | {"market_result": "void"}  # unknown_result
    for order in ([bad_value, bad_result], [bad_result, bad_value]):
        h.double.settlement_log[-1:] = []
        h.double.settlement_log.extend(order)
        unreadable = [
            item
            for item in h.gateway.settlements_since(None)[0]
            if isinstance(item, UnreadableSettlement) and item.token == TICKER
        ]
        assert [u.error for u in unreadable] == ["inconsistent_settlement"]
        assert len(unreadable[0].fingerprints) == 2
        h.double.settlement_log[-2:] = [record]


def test_the_exact_repeated_resolved_pair_stays_clean(tmp_path):
    h, first, held = resolved_pair(tmp_path)
    settled_cleanly(h, 3900)
    h.restart()
    settled_cleanly(h, 3950)


def test_duplicate_and_reordered_listings_are_the_same_evidence(tmp_path):
    """The venue repeats and reorders the two resolved records: same fingerprints, so
    the acknowledgement still covers exactly them and nothing is reported."""
    h, first, held = resolved_pair(tmp_path)
    before = evidence(h, TICKER)
    log = h.double.settlement_log
    log[:] = [dict(log[-1]), dict(log[0]), dict(log[0]), dict(log[-1])]
    assert evidence(h, TICKER) == before
    settled_cleanly(h, 3900)
    h.restart()
    settled_cleanly(h, 3950)


def test_changed_content_under_the_same_source_time_is_a_new_record(tmp_path):
    """The venue keeps the resolved YES record's settled time but changes its content:
    a different fingerprint, not covered, and in conflict with the ledger."""
    h, first, held = resolved_pair(tmp_path)
    assert "settled_time" in first
    first["market_result"], first["value"], first["revenue"] = "scalar", 50, int(held * 50)
    report = h.executor.reconcile(now=h.at(3900))
    assert "settlement_conflict:" + TICKER in report.unresolved, report.unresolved
    h.restart()
    assert "settlement_conflict:" + TICKER in h.executor.reconcile(now=h.at(3950)).unresolved


def test_an_unparseable_record_beside_a_readable_one_is_not_read_as_the_settlement(tmp_path):
    """One ticker lists a void record and a readable YES record: neither is recorded,
    both are evidence, and the readable one is only skipped when acknowledged itself."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    readable = h.double.settle(TICKER, "yes")
    h.double.settlement_log.append(readable | {"market_result": "void"})
    report = h.executor.reconcile(now=h.at(3700))
    assert "settlement_unreadable:" + TICKER in report.unresolved
    assert "position_mismatch:" + TICKER in report.unresolved  # settled at the venue only
    assert h.ledger.settlements() == []
    seen = evidence(h, TICKER)
    assert sorted(v.get("error", v.get("result")) for v in seen.values()) == [
        "unknown_result",
        "yes",
    ]
    assert [v["as_listed"]["market_result"] for v in seen.values() if "error" in v] == ["void"]
    void_only = {fp: v for fp, v in seen.items() if "error" in v}
    acknowledge_settlement(
        h.ledger, TICKER, actor="operator", note="void", evidence=void_only, at=h.at(3750)
    )
    # A group never records: the readable record stays reported until the operator
    # resolves it too, and the settlement comes from the operator, never the sibling.
    report = h.executor.reconcile(now=h.at(3800))
    assert "settlement_unreadable:" + TICKER in report.unresolved, report.unresolved
    assert h.ledger.settlements() == []
    acknowledge(h, TICKER, "both reviewed", 3850)
    h.ledger.append_settlement(
        Settlement("operator:" + TICKER, TICKER, "yes", "1", "operator:investigated", at(3600))
    )
    settled_cleanly(h, 3900)
    assert [s.source_ref for s in h.ledger.settlements()] == ["operator:investigated"]


def test_a_gateway_that_names_no_evidence_cannot_be_acknowledged_away(tmp_path):
    """An unreadable record without records or fingerprints carries nothing an
    acknowledgement could name: it stays reported."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    h.double.settle(VOID, "void")
    assert not h.executor.reconcile(now=h.at(3700)).clean
    acknowledge(h, VOID, "void", 3750)
    settled_cleanly(h, 3800)
    read = h.gateway.settlements_since
    h.gateway.settlements_since = lambda cursor: (
        [UnreadableSettlement(VOID, "unknown_result") for _ in read(cursor)[0]],
        read(cursor)[1],
    )
    assert h.executor.reconcile(now=h.at(3850)).unresolved == ("settlement_unreadable:" + VOID,)


def test_settlement_evidence_describes_each_record(tmp_path):
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    record = h.double.settle(TICKER, "yes")
    seen = evidence(h, TICKER)
    parsed = settlement_from_exchange(record)
    assert seen == {
        settlement_fingerprint(parsed): {
            "result": "yes",
            "settlement_value": "1",
            "yes_count": str(held),
            "no_count": "0",
            "revenue": str(held),
            "fee": "0",
            "settled_at": parsed.settled_at.isoformat(),
            "source_ref": parsed.source_ref,
        }
    }
    assert settlement_fingerprint(parsed).startswith("vs1:")
    assert settlement_ack_key(TICKER, seen) == settlement_ack_key(TICKER, list(seen))
    assert settlement_ack_key(TICKER, seen) != settlement_ack_key(TICKER)


def test_an_acknowledged_market_awaiting_the_operators_settlement(tmp_path):
    """An acknowledgement of this ledger's own market that the ledger has not settled
    covers the record the operator saw; the open holding stays visible through the
    position and balance checks until the operator settles the ledger. A revised record
    is not covered and is reported on its own merits."""
    h, held = holding_one_fill(tmp_path)
    assert h.executor.reconcile(now=h.at(20)).clean
    record = h.double.settle(TICKER, "yes", fee="0.01")
    assert "settlement_fee:" + TICKER in h.executor.reconcile(now=h.at(3700)).unresolved
    acknowledge(h, TICKER, "investigating", 3750)
    report = h.executor.reconcile(now=h.at(3800))
    assert not any(r.startswith("settlement_") for r in report.unresolved)
    assert "position_mismatch:" + TICKER in report.unresolved
    record["market_result"] = "no"
    h.double.cash -= held
    report = h.executor.reconcile(now=h.at(3900))
    assert {"settlement_fee:" + TICKER, "balance_mismatch", "position_mismatch:" + TICKER} <= set(
        report.unresolved
    )
    assert h.ledger.settlements() == []
