"""KalshiGateway against the SYNTHETIC exchange double, mostly through the Executor:
accept, reject, partial fills, cancel races, duplicate events, read backoff and no
write retry, timeouts on create with reconciliation, restart, and startup mismatches
that block risk-increasing orders."""

import pytest
from _trading_synthetic import TICKER, D, approve, at, book, make_intent, market

from vader_intelligence.trading.executor import FILLS_CURSOR
from vader_intelligence.trading.kalshi.gateway import (
    ForeignActivity,
    LookupInconclusive,
    MarketRule,
    ReconciliationMismatch,
)
from vader_intelligence.trading.kalshi.mapping import price_ranges
from vader_intelligence.trading.orders import Order
from vader_intelligence.trading.risk import Approval, ForecastStatus, Rejection

from ._kalshi_double import LINEAR_CENT, PREFIX, Harness

CREATE = PREFIX + "/portfolio/events/orders"


def place(h, t, probability="0.55", **intent_options):
    now = h.at(t)
    intent = make_intent(t=t, probability=probability, **intent_options)
    return h.executor.place(
        intent,
        now=now,
        market=market(),
        snapshot=book(t=t - 5),
        forecast=ForecastStatus(TICKER, D(probability), at(t - 5), "synthetic-model"),
        marks={TICKER: book(t=t - 5)},
        equity_history=((at(-30), D(1000)),),
    )


def state(h, coid):
    return h.ledger.projection().orders[coid]


def posts(h):
    return h.double.calls_to("POST", "/portfolio/events/orders")


def test_accept_partial_fills_and_reconcile(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc", post_only=True)
    assert isinstance(placement.decision, Approval)
    assert placement.submit.status == "accepted"
    coid, order_id = placement.order.client_order_id, placement.submit.exchange_order_id
    (call,) = posts(h)
    assert call[3] == {
        "ticker": TICKER,
        "side": "bid",
        "price": "0.4600",
        "count": f"{placement.order.quantity}.00",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": coid,
        "post_only": True,
        "reduce_only": False,
        "exchange_index": 3,
    }
    assert state(h, coid).status == "open"
    h.at(15)
    h.double.execute(order_id, "10", fee="0.02")
    assert h.executor.reconcile(now=h.at(20)).clean
    partial = state(h, coid)
    assert partial.status == "open" and partial.filled_quantity == D(10)
    assert h.ledger.projection().portfolio.cash == D(1000) - D("4.60") - D("0.02")
    rest = placement.order.quantity - 10
    h.double.execute(order_id, rest, fee="0.05")
    assert h.executor.reconcile(now=h.at(30)).clean
    assert state(h, coid).status == "filled"
    assert h.ledger.projection().portfolio.cash == h.double.cash
    assert h.ledger.verify() == [] and h.double.auth_failures == 0


def test_buy_no_maps_to_ask_and_reconciles(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, side="no", price="0.54", probability="0.35", tif="gtc")
    assert placement.submit.status == "accepted"
    (call,) = posts(h)
    assert (call[3]["side"], call[3]["price"]) == ("ask", "0.4600")
    h.double.execute(placement.submit.exchange_order_id, "6", yes_price="0.47", fee="0.03")
    assert h.executor.reconcile(now=h.at(20)).clean
    (fill,) = h.ledger.fills()
    assert (fill.side, fill.action, fill.price) == ("no", "buy", D("0.53"))
    assert h.ledger.projection().portfolio.holding(TICKER).no_qty == D(6)
    assert h.double.positions[TICKER] == D(-6)


def test_ioc_partial_fill_then_remainder_canceled(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.immediate = lambda order: [("10", None, "0.18")]
    placement = place(h, 10)
    assert placement.submit.status == "accepted"
    (call,) = posts(h)
    assert call[3]["time_in_force"] == "immediate_or_cancel"
    report = h.executor.reconcile(now=h.at(20))
    assert report.clean and placement.order.client_order_id in report.reconciled
    final = state(h, placement.order.client_order_id)
    assert final.status == "canceled" and final.filled_quantity == D(10)
    assert h.ledger.projection().portfolio.reserved_cash == 0
    assert h.ledger.verify() == []


def test_exchange_rejection_is_definitive(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.reject_create = "insufficient_balance"
    placement = place(h, 10)
    assert placement.submit.status == "rejected"
    assert placement.submit.reason == "exchange:insufficient_balance"
    assert state(h, placement.order.client_order_id).status == "rejected"
    assert h.ledger.projection().portfolio.reserved_cash == 0
    assert h.executor.reconcile(now=h.at(20)).clean


def test_local_refusals_send_nothing(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    _, order = approve(h.ledger, make_intent(t=1, rationale="direct"), "5", t=1)
    assert h.gateway.submit(order).reason == "local:startup_reconciliation_pending"
    h.gateway.positions(), h.gateway.balance()
    assert h.gateway.startup_reconciled
    stranger = Order(**{**order.to_dict(), "quantity": "6"})
    assert h.gateway.submit(stranger).reason == "local:order_not_in_ledger"
    paper = Order(**{**order.to_dict(), "client_order_id": "vi-paper-0123456789abcdef-1"})
    assert h.gateway.submit(paper).reason == "local:not_a_live_order"
    h.ledger.set_kill_switch(True, "operator test", "test-operator", at(2))
    assert h.gateway.submit(order).reason == "local:kill_switch_engaged"
    h.ledger.set_kill_switch(False, "operator test", "test-operator", at(3))
    capped = h.make_gateway(max_order_notional="2", max_daily_notional="200")
    capped.positions(), capped.balance()
    assert capped.submit(order).reason == "local:order_notional_cap"  # 5 x 0.46 = 2.30
    daily = h.make_gateway(max_order_notional="3", max_daily_notional="3")
    daily.positions(), daily.balance()
    _, second = approve(h.ledger, make_intent(t=4, rationale="second"), "2", t=4)
    assert daily.submit(second).reason == "local:daily_notional_cap"  # 2.30 + 0.92
    other = MarketRule("OTHER", price_ranges(LINEAR_CENT))
    elsewhere = h.make_gateway(markets=(other,))
    elsewhere.positions(), elsewhere.balance()
    assert elsewhere.submit(order).reason == "local:ticker_not_allowed"
    h.gateway.halt("operator shutdown")
    assert h.gateway.submit(order).reason == "local:halted"
    assert posts(h) == []


def test_sell_not_covered_by_the_venue_position_is_refused(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    buy = place(h, 10, tif="gtc")
    h.double.execute(buy.submit.exchange_order_id, buy.order.quantity, fee="0.10")
    assert h.executor.reconcile(now=h.at(12)).clean
    first = place(h, 14, action="sell", price="0.50", qty="4", tif="gtc", rationale="a")
    assert first.submit.status == "accepted"
    h.double.positions[TICKER] = D(5)  # the venue now disagrees with the ledger
    report = h.executor.reconcile(now=h.at(15))
    assert f"position_mismatch:{TICKER}" in report.unresolved  # buys are now blocked
    second = place(h, 16, action="sell", price="0.50", qty="2", tif="gtc", rationale="b")
    assert isinstance(second.decision, Approval)  # risk lets reduce-only sells through
    # 2 + 4 already resting > 5 held at the venue: the ask could open a NO position.
    assert second.submit.reason == "local:venue_position_insufficient"
    assert len(posts(h)) == 2


def test_cancel_reports_canceled_only_when_fills_are_retrievable(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc")
    coid, order_id = placement.order.client_order_id, placement.submit.exchange_order_id
    h.at(12)
    h.double.execute(order_id, "5", fee="0.09", hidden=True)  # not yet visible over REST
    result = h.executor.cancel(coid, now=h.at(13))
    assert (result.status, result.reason) == ("pending", "fills_not_yet_retrievable")
    held = state(h, coid)
    assert held.status == "open" and held.cancel_requested  # reservation kept
    assert h.ledger.projection().portfolio.reserved_cash > 0
    h.double.publish()
    assert h.executor.reconcile(now=h.at(20)).clean
    final = state(h, coid)
    assert final.status == "canceled" and final.filled_quantity == D(5)
    assert h.ledger.projection().portfolio.reserved_cash == 0
    assert h.ledger.verify() == []


def test_fill_arriving_with_the_cancel_request(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc")
    coid = placement.order.client_order_id
    h.double.on_cancel = lambda order: h.double.execute(order["order_id"], "4", fee="0.07")
    result = h.executor.cancel(coid, now=h.at(12))
    # Canceled at the venue with a fill the ledger does not hold yet: releasing the
    # reservation now would understate exposure, so the outcome is left to reconcile.
    assert (result.status, result.reason) == ("pending", "fills_not_yet_recorded")
    assert state(h, coid).status == "open" and state(h, coid).cancel_requested
    assert h.executor.reconcile(now=h.at(20)).clean
    final = state(h, coid)
    assert final.status == "canceled" and final.filled_quantity == D(4)
    assert h.ledger.projection().portfolio.reserved_cash == 0
    assert h.ledger.verify() == []


def test_cancel_after_fills_were_recorded_is_canceled_at_once(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc")
    coid = placement.order.client_order_id
    h.double.execute(placement.submit.exchange_order_id, "4", fee="0.07")
    assert h.executor.reconcile(now=h.at(12)).clean  # the ledger now holds the 4
    result = h.executor.cancel(coid, now=h.at(13))
    assert (result.status, result.reason) == ("canceled", "canceled")
    final = state(h, coid)
    assert final.status == "canceled" and final.filled_quantity == D(4)
    assert h.ledger.projection().portfolio.reserved_cash == 0
    assert h.executor.reconcile(now=h.at(14)).clean and h.ledger.verify() == []


def test_cancel_after_complete_fill_is_not_a_cancel(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc")
    coid, quantity = placement.order.client_order_id, placement.order.quantity
    h.double.on_cancel = lambda order: h.double.execute(order["order_id"], quantity)
    result = h.executor.cancel(coid, now=h.at(12))
    assert (result.status, result.reason) == ("rejected", "already_executed")
    assert h.executor.reconcile(now=h.at(20)).clean
    assert state(h, coid).status == "filled"


def test_duplicate_events_are_idempotent(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc")
    order_id = placement.submit.exchange_order_id
    for i in range(3):
        h.at(11 + i)
        h.double.execute(order_id, "2", fee="0.01")
    h.double.duplicate_pages = True  # every page repeats the last item of the previous one
    fills, cursor = h.gateway.fills_since(None)
    assert len(fills) == 3 and len({f.fill_id for f in fills}) == 3
    assert h.executor.reconcile(now=h.at(20)).clean
    assert h.executor.reconcile(now=h.at(21)).clean  # the overlap re-reads the same fills
    h.executor.ingest(fills + fills, now=h.at(22))  # duplicates through ingest as well
    assert (
        len(h.ledger.fills()) == 3
        and state(h, placement.order.client_order_id).filled_quantity == 6
    )
    conflicting = dict(h.double.fill_log[0], count_fp="3.00")
    h.double.fill_log.append(conflicting)
    with pytest.raises(ReconciliationMismatch):
        h.gateway.fills_since(cursor)
    assert "fills_unavailable" in h.executor.reconcile(now=h.at(30)).unresolved


def test_reads_back_off_and_create_is_never_retried(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.inject("GET", PREFIX + "/portfolio/balance", 429, times=2)
    assert h.executor.reconcile(now=h.at(1)).clean
    assert len(h.double.calls_to("GET", "/portfolio/balance")) == 3
    # Backoff delays 0.5 s and 1.0 s, each followed by the wait the balance bucket (one
    # request per 2 s) still imposes before the next attempt.
    assert h.clock.sleeps == [0.5, 1.5, 1.0, 1.0]
    h.double.inject("POST", CREATE, 429)
    first = place(h, 10)
    assert first.submit.status == "unknown" and len(posts(h)) == 1
    coid = first.order.client_order_id
    assert state(h, coid).status == "unknown"
    blocked = place(h, 12, rationale="second")  # reconcile first: lookup not definitive yet
    assert isinstance(blocked.decision, Rejection) and "state_unknown" in blocked.decision.reasons
    assert len(posts(h)) == 1  # nothing new was sent, nothing was retried
    report = h.executor.reconcile(now=h.at(60))  # past the settle window: definitively absent
    assert report.clean and state(h, coid).status == "rejected"
    assert h.double.orders == {}
    h.double.inject("POST", CREATE, 503)
    third = place(h, 70, rationale="third")
    assert third.submit.status == "unknown" and len(posts(h)) == 2


def test_timeout_on_create_then_reconciliation_finds_the_order(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.inject("POST", CREATE, "timeout_after")  # processed, response lost
    placement = place(h, 10, tif="gtc")
    coid = placement.order.client_order_id
    assert placement.submit.status == "unknown" and state(h, coid).status == "unknown"
    report = h.executor.reconcile(now=h.at(12))
    assert report.clean and coid in report.reconciled
    resolved = state(h, coid)
    assert resolved.status == "open" and resolved.exchange_order_id is not None
    listing = h.double.calls_to("GET", "/portfolio/orders")
    assert any(c[2].get("ticker") == TICKER and "min_ts" in c[2] for c in listing)
    assert len(h.double.orders_with(coid)) == 1 and len(posts(h)) == 1  # no duplicate
    assert place(h, 10, tif="gtc").existing  # the same intent is never resubmitted
    assert len(posts(h)) == 1


def test_timeout_before_processing_none_then_safe_same_id_resubmission(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.inject("POST", CREATE, "timeout_before")  # never reached the venue
    placement = place(h, 10, tif="gtc")
    order, coid = placement.order, placement.order.client_order_id
    assert placement.submit.status == "unknown"
    with pytest.raises(LookupInconclusive, match="settle"):
        h.gateway.lookup(coid)  # inside the settle window: not definitive
    h.at(45)
    assert h.gateway.lookup(coid) is None  # whole window scanned, settle elapsed
    again = h.gateway.submit(order)  # same Order, same client_order_id
    assert again.status == "accepted"
    assert len(h.double.orders_with(coid)) == 1
    third = h.gateway.submit(order)  # duplicate -> 409 -> lookup, never a second order
    assert third.status == "accepted" and third.exchange_order_id == again.exchange_order_id
    assert third.reason == "duplicate_client_order_id_resolved_by_lookup"
    assert len(h.double.orders_with(coid)) == 1 and len(posts(h)) == 3


def test_lost_response_with_rest_lag_resubmission_hits_409_then_lookup(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.lag = True  # created orders are not visible over REST until published
    h.double.inject("POST", CREATE, "timeout_after")
    placement = place(h, 10, tif="gtc")
    order, coid = placement.order, placement.order.client_order_id
    h.at(45)
    assert h.gateway.lookup(coid) is None  # the listing lags behind the matching engine
    with pytest.raises(LookupInconclusive, match="409"):
        h.gateway.submit(order)  # the exchange knows the id: 409, but lookup cannot see it
    assert len(h.double.orders_with(coid)) == 1
    h.double.publish()
    result = h.gateway.submit(order)
    (venue,) = h.double.orders_with(coid)
    assert result.status == "accepted" and result.exchange_order_id == venue["order_id"]
    assert len(posts(h)) == 3 and len(h.double.orders) == 1


def test_lagging_order_marked_absent_is_caught_as_a_stray(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.lag = True
    h.double.inject("POST", CREATE, "timeout_after")
    coid = place(h, 10, tif="gtc").order.client_order_id
    h.double.lag = False
    assert h.executor.reconcile(now=h.at(45)).clean  # REST still lags: venue says absent
    assert state(h, coid).status == "rejected"
    h.double.publish()  # the order surfaces: an unexplained resting order
    report = h.executor.reconcile(now=h.at(50))
    assert "positions_unavailable" in report.unresolved
    blocked = place(h, 52, rationale="after stray")
    assert "state_unknown" in blocked.decision.reasons and len(posts(h)) == 1


def test_restart_with_resting_order_and_fills_since_cursor(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc")
    coid, order_id = placement.order.client_order_id, placement.submit.exchange_order_id
    h.at(15)
    h.double.execute(order_id, "5", fee="0.09")
    assert h.executor.reconcile(now=h.at(20)).clean
    cursor = h.ledger.processed(FILLS_CURSOR)["cursor"]
    assert cursor.startswith("ts:")
    h.at(30)
    h.double.execute(order_id, "7", fee="0.12")  # while the process is down
    h.restart()
    calls_before = len(h.double.calls)
    assert h.executor.reconcile(now=h.at(40)).clean
    session = h.double.calls[calls_before:]
    assert any(c[1] == PREFIX + "/portfolio/orders" and "ticker" in c[2] for c in session)
    fill_reads = [c for c in session if c[1] == PREFIX + "/portfolio/fills"]
    assert int(fill_reads[0][2]["min_ts"]) >= int(at(-3600).timestamp())
    resumed = state(h, coid)
    assert resumed.status == "open" and resumed.filled_quantity == D(12)
    assert len(h.ledger.fills()) == 2
    assert h.ledger.processed(FILLS_CURSOR)["cursor"] >= cursor
    assert h.ledger.verify() == []
    assert len(posts(h)) == 1  # the resting order was never resubmitted


@pytest.mark.parametrize("problem", ["stray", "position", "balance", "foreign_fill"])
def test_startup_reconciliation_mismatch_blocks_buys(tmp_path, problem):
    h = Harness(tmp_path / "l.sqlite3")
    if problem == "stray":
        h.double.seed_order("vi-live-ffffffffffffffff-1", TICKER, "bid", "0.40", "3")
        expected = "positions_unavailable"
    elif problem == "position":
        h.double.positions[TICKER] = D(5)
        expected = f"position_mismatch:{TICKER}"
    elif problem == "balance":
        h.double.cash += D("0.01")
        expected = "balance_mismatch"
    else:
        cash = h.double.cash
        foreign = h.double.seed_order(None, TICKER, "ask", "0.50", "2")  # e.g. a manual order
        h.double.execute(foreign, "2")
        h.double.cash, h.double.positions = cash, {}  # isolate the fill from other checks
        expected = "fills_unavailable"
    blocked = place(h, 10)
    assert isinstance(blocked.decision, Rejection)
    assert "state_unknown" in blocked.decision.reasons
    assert expected in h.executor.unknown_reasons
    assert posts(h) == []
    if problem == "foreign_fill":
        with pytest.raises(ForeignActivity):
            h.gateway.fills_since(None)


def test_duplicate_exposure_and_content_mismatch_are_detected(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc")
    coid = placement.order.client_order_id
    h.double.seed_order(coid, TICKER, "bid", "0.46", placement.order.quantity)
    h.restart()  # no remembered exchange id: lookup scans the listing
    with pytest.raises(ReconciliationMismatch, match="duplicate exposure"):
        h.gateway.lookup(coid)
    assert f"lookup_failed:{coid}" in h.executor.reconcile(now=h.at(20)).unresolved
    other = Harness(tmp_path / "m.sqlite3")
    _, order = approve(other.ledger, make_intent(t=1, rationale="m"), "5", t=1)
    other.double.seed_order(order.client_order_id, TICKER, "bid", "0.45", "5")
    with pytest.raises(ReconciliationMismatch, match="yes_price"):
        other.gateway.lookup(order.client_order_id)


def test_lookup_windows_and_listing_parameters(tmp_path):
    h = Harness(tmp_path / "l.sqlite3", settle_seconds=30, max_lookup_age_seconds=600)
    _, order = approve(h.ledger, make_intent(t=0, rationale="w"), "5", t=0)
    coid = order.client_order_id
    with pytest.raises(LookupInconclusive, match="settle"):
        h.gateway.lookup(coid)
    h.at(31)
    assert h.gateway.lookup(coid) is None
    listing = h.double.calls_to("GET", "/portfolio/orders")[-1][2]
    assert listing["ticker"] == TICKER
    assert int(listing["min_ts"]) == int(at(-300).timestamp())
    h.at(601)
    with pytest.raises(LookupInconclusive, match="listing window"):
        h.gateway.lookup(coid)
    with pytest.raises(LookupInconclusive):
        h.gateway.lookup("vi-live-0000000000000000-1")  # not an order of this ledger


def test_balance_per_shard_and_exchange_status(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    assert h.gateway.balance() == D(1000)
    assert h.gateway.shard_balances == {0: D(0), 3: D(1000)}  # MLB markets: shard 3
    status = h.gateway.exchange_status()
    assert status["trading_active"] and status["shards"][3] is True
    h.double.zero_position_rows = (TICKER,)
    h.double.positions[TICKER] = D(0)
    assert h.gateway.positions() == {}  # zero rows are not holdings
