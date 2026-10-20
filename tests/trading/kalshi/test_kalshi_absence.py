"""Venue absence is never taken on one read (fixes after an independent review; SYNTHETIC
exchange double, throwaway keys, temp dirs; nothing is sent anywhere).

- F32: an order the exchange accepted (the ledger records the 201 and its exchange order
  id) is never reported "not found" by `lookup`, also after a restart.
"""

import pytest

from vader_intelligence.trading.gateway import GatewayEvent
from vader_intelligence.trading.kalshi.gateway import LookupInconclusive, ReconciliationMismatch

from ._kalshi_double import Harness
from .test_kalshi_gateway import place, state


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
