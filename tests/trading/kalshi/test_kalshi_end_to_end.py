"""End to end through the Executor with the SYNTHETIC exchange double: intent ->
approval -> order -> fills -> positions reconcile -> kill switch cancels open orders."""

from _trading_synthetic import TICKER, D, at, book, make_intent, market

from vader_intelligence.trading.kalshi.shutdown import shutdown
from vader_intelligence.trading.risk import Approval, ForecastStatus, Rejection

from ._kalshi_double import Harness


def place(h, t, **intent_options):
    now = h.at(t)
    return h.executor.place(
        make_intent(t=t, **intent_options),
        now=now,
        market=market(),
        snapshot=book(t=t - 5),
        forecast=ForecastStatus(TICKER, D("0.55"), at(t - 5), "synthetic-model"),
        marks={TICKER: book(t=t - 5)},
        equity_history=((at(-30), D(1000)),),
    )


def test_intent_to_fills_to_kill_switch(tmp_path):
    h = Harness(tmp_path / "live.sqlite3")
    buy = place(h, 10, tif="gtc", post_only=True)
    assert isinstance(buy.decision, Approval) and buy.submit.status == "accepted"
    buy_coid = buy.order.client_order_id
    h.at(15)
    h.double.execute(buy.submit.exchange_order_id, "10", fee="0.02")
    assert h.executor.reconcile(now=h.at(20)).clean
    portfolio = h.ledger.projection().portfolio
    assert portfolio.holding(TICKER).yes_qty == D(10) == h.double.positions[TICKER]
    assert portfolio.cash == h.double.cash == D(1000) - D("4.60") - D("0.02")
    sell = place(h, 25, action="sell", price="0.50", qty="4", tif="gtc", rationale="exit")
    assert isinstance(sell.decision, Approval) and sell.submit.status == "accepted"
    sell_body = h.double.calls_to("POST", "/portfolio/events/orders")[-1][3]
    assert (sell_body["side"], sell_body["price"], sell_body["count"]) == ("ask", "0.5000", "4.00")
    active = {s.order.client_order_id for s in h.ledger.projection().active_orders}
    assert active == {buy_coid, sell.order.client_order_id}

    report = shutdown(h.executor, now=h.at(30), reason="operator kill switch", actor="operator")

    assert report.clean, report.to_dict()
    assert {c.split(":")[0] for c in report.canceled} == active
    assert all(o["status"] in ("canceled", "executed") for o in h.double.orders.values())
    assert h.gateway.halted and h.ledger.kill_switch().engaged
    projection = h.ledger.projection()
    assert projection.active_orders == [] and projection.portfolio.reserved_cash == 0
    assert projection.portfolio.holding(TICKER).yes_qty == D(10)  # orders canceled, holdings kept
    assert h.ledger.verify() == []
    assert any(k.startswith("shutdown:") for k in h.ledger.processed_inputs())
    posts = len(h.double.calls_to("POST", "/portfolio/events/orders"))
    after = place(h, 40, rationale="after the kill switch")
    assert isinstance(after.decision, Rejection) and "kill_switch_engaged" in after.decision.reasons
    assert h.gateway.submit(sell.order).reason == "local:halted"
    assert len(h.double.calls_to("POST", "/portfolio/events/orders")) == posts
    assert h.double.auth_failures == 0 and h.double.unknown == []


def test_shutdown_cancels_strays_and_reports_foreign_orders(tmp_path):
    h = Harness(tmp_path / "live.sqlite3")
    resting = place(h, 10, tif="gtc")
    stray = h.double.seed_order("vi-live-eeeeeeeeeeeeeeee-1", TICKER, "bid", "0.40", "2")
    foreign = h.double.seed_order(None, TICKER, "bid", "0.30", "1")  # e.g. a manual order
    report = shutdown(h.executor, now=h.at(20), reason="shutdown", actor="operator")
    assert report.canceled == (f"{resting.order.client_order_id}:canceled",)
    assert report.strays_canceled == (stray,)
    assert report.foreign_resting == (foreign,)
    assert h.double.orders[foreign]["status"] == "resting"  # never touched
    assert not report.clean and "positions_unavailable" in report.unresolved
    assert h.double.orders[stray]["status"] == "canceled"
