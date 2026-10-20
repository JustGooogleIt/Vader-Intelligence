"""Venue absence is never taken on one read (fixes after an independent review; SYNTHETIC
exchange double, throwaway keys, temp dirs; nothing is sent anywhere).

- F32: an order the exchange accepted (the ledger records the 201 and its exchange order
  id) is never reported "not found" by `lookup`, also after a restart.
- F30: a create whose outcome is unknown stays `unknown` (blocking buys) after one
  negative read; only a second one after the grace period, with an open-orders sweep
  that does not list it, is final. A venue-open order that is not an active ledger
  order blocks buys (`venue_open_untracked:<coid>`), and the executor can cancel an
  order whose terminal state came only from that absence.
"""

import pytest
from _trading_synthetic import D

from vader_intelligence.trading.executor import ABSENCE_GRACE_SECONDS
from vader_intelligence.trading.gateway import GatewayEvent
from vader_intelligence.trading.kalshi.gateway import LookupInconclusive, ReconciliationMismatch
from vader_intelligence.trading.risk import Approval, Rejection

from ._kalshi_double import Harness
from .test_kalshi_gateway import CREATE, place, posts, state


def test_an_accepted_order_is_never_not_found_after_a_restart(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.lag = True  # REST reads lag behind the matching engine
    placement = place(h, 10, tif="gtc")
    coid, order_id = placement.order.client_order_id, placement.submit.exchange_order_id
    assert placement.submit.status == "accepted"
    assert (state(h, coid).status, state(h, coid).exchange_order_id) == ("open", order_id)
    reserved = h.ledger.projection().portfolio.reserved_cash
    assert reserved > 0
    h.restart()  # a new process: nothing of the 201 is left in gateway memory
    h.at(70)  # well past the settle window
    with pytest.raises(LookupInconclusive, match="accepted"):
        h.gateway.lookup(coid)
    assert h.double.calls_to("GET", f"/portfolio/orders/{order_id}")  # by the recorded id
    report = h.executor.reconcile(now=h.at(70))
    assert f"lookup_failed:{coid}" in report.unresolved and not report.clean
    assert state(h, coid).status == "open"
    assert h.ledger.projection().portfolio.reserved_cash == reserved
    assert h.executor.cancel(coid, now=h.at(72)).status != "canceled"  # still lagging
    assert state(h, coid).status == "open"
    h.double.publish()
    h.restart()
    assert h.executor.cancel(coid, now=h.at(75)).status == "canceled"  # by recorded id
    assert state(h, coid).status == "canceled" and h.double.orders[order_id]["status"] == (
        "canceled"
    )
    assert h.executor.reconcile(now=h.at(80)).clean
    assert h.ledger.verify() == []
    other = "00000000-0000-4000-8000-0000000000ff"  # a second exchange id for the same order
    event = GatewayEvent("ev-x", coid, "accepted", exchange_order_id=other)
    h.executor.ingest([event], now=h.at(81))
    h.restart()
    with pytest.raises(ReconciliationMismatch, match="duplicate exposure"):
        h.gateway.lookup(coid)


def test_one_negative_read_never_finalizes_an_unknown_submission(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.lag = True  # created orders stay invisible to REST reads until published
    h.double.inject("POST", CREATE, "timeout_after")  # processed, response lost
    first = place(h, 10, tif="gtc")
    coid = first.order.client_order_id
    assert first.submit.status == "unknown"
    reserved = h.ledger.projection().portfolio.reserved_cash
    report = h.executor.reconcile(now=h.at(45))  # past the settle window, still lagging
    assert f"absence_unconfirmed:{coid}" in report.unresolved and not report.clean
    assert state(h, coid).status == "unknown"
    assert h.ledger.projection().portfolio.reserved_cash == reserved
    second = place(h, 50, rationale="second")  # reconciles again first: still unconfirmed
    assert isinstance(second.decision, Rejection) and "state_unknown" in second.decision.reasons
    assert len(posts(h)) == 1
    # A second negative read after the grace period, with an open-orders sweep that does
    # not list the order, is final (here the venue still lags: the worst case).
    t = 45 + ABSENCE_GRACE_SECONDS
    report = h.executor.reconcile(now=h.at(t))
    assert report.clean and coid in report.reconciled
    assert state(h, coid).status == "rejected"
    assert h.ledger.projection().portfolio.reserved_cash == 0
    # The order surfaces and rests: venue-open but terminal in the ledger blocks buys.
    h.double.publish()
    (venue,) = h.double.orders_with(coid)
    report = h.executor.reconcile(now=h.at(t + 5))
    assert f"venue_open_untracked:{coid}" in report.unresolved
    blocked = place(h, t + 10, rationale="third")
    assert "state_unknown" in blocked.decision.reasons and len(posts(h)) == 1
    # Its fill is still recorded (exposure and cash stay exact) ...
    h.double.execute(venue["order_id"], "3", fee="0.05")
    assert f"venue_open_untracked:{coid}" in h.executor.reconcile(now=h.at(t + 15)).unresolved
    assert state(h, coid).filled_quantity == D(3)
    # ... and the executor can cancel it, although the ledger already calls it terminal.
    result = h.executor.cancel(coid, now=h.at(t + 20))
    assert result is not None and result.status == "canceled"
    assert h.double.orders[venue["order_id"]]["status"] == "canceled"
    assert state(h, coid).status == "rejected"  # a terminal state never reopens
    assert h.executor.cancel(coid, now=h.at(t + 21)) is None  # the venue confirmed its end
    assert h.executor.reconcile(now=h.at(t + 25)).clean
    assert h.ledger.verify() == []
    fourth = place(h, t + 30, rationale="fourth")
    assert isinstance(fourth.decision, Approval) and len(posts(h)) == 2
