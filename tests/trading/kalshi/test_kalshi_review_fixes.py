"""Live-adapter protections added after the independent review of #25 (SYNTHETIC exchange
double, throwaway keys, temp dirs; nothing is sent anywhere): ambiguous write answers,
uncapped covered exits, sell coverage net of recent fills, limits bound at activation,
the ledger bound to one environment and key, strays keep startup unreconciled, and a
trust store that ignores the environment."""

from dataclasses import replace

import httpx
import pytest
from _trading_synthetic import TICKER, D, approve, at, make_intent, submit_requested
from cryptography.hazmat.primitives.asymmetric import ed25519

from vader_intelligence.trading.executor import ABSENCE_GRACE_SECONDS
from vader_intelligence.trading.gateway import GatewayEvent
from vader_intelligence.trading.kalshi.auth import Signer
from vader_intelligence.trading.kalshi.client import (
    KalshiClient,
    RequestRejected,
    WriteOutcomeUnknown,
)
from vader_intelligence.trading.kalshi.gateway import ReconciliationMismatch

from ._kalshi_double import KEY_ID, FakeClock, Harness
from .test_kalshi_activation import Setup
from .test_kalshi_gateway import place

ORDER = {"ticker": "SYN-X-Y", "side": "bid", "count": "1.00", "price": "0.5000"}


@pytest.mark.parametrize(
    "status,error",
    [
        (400, RequestRejected),
        (403, RequestRejected),
        (422, RequestRejected),
        (408, WriteOutcomeUnknown),
        (425, WriteOutcomeUnknown),
        (451, WriteOutcomeUnknown),
        (302, WriteOutcomeUnknown),
    ],
)
def test_only_documented_write_rejections_are_definitive(status, error):
    clock = FakeClock()
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    client = KalshiClient(
        "demo",
        Signer(KEY_ID, ed25519.Ed25519PrivateKey.generate(), clock_ms=clock.ms),
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    with pytest.raises(error):
        client.request("POST", "/portfolio/events/orders", body=ORDER)


def test_covered_exits_are_not_capped_and_sells_net_recent_fills(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    buy = place(h, 10, tif="gtc")
    held = buy.order.quantity
    h.double.execute(buy.submit.exchange_order_id, held, fee="0.10")
    assert h.executor.reconcile(now=h.at(12)).clean  # positions read: `held` contracts
    capped = h.make_gateway(max_order_notional="0.10", max_daily_notional="0.10")
    capped.positions(), capped.balance()

    def sell(t, why):
        intent = make_intent(
            t=t, action="sell", price="0.40", qty=str(held), tif="gtc", rationale=why
        )
        return approve(h.ledger, intent, str(held), t=t)[1]

    exit_ = sell(14, "exit")
    h.ledger.append_event(submit_requested(exit_, t=14))  # as the executor records first
    assert capped.submit(exit_).status == "accepted"  # above both caps, still an exit
    _, more = approve(h.ledger, make_intent(t=15, rationale="more"), "1", t=15)
    assert capped.submit(more).reason == "local:order_notional_cap"  # buys stay capped
    # The exit fills; its fills are ingested but positions are not read again.
    h.double.execute(capped.lookup(exit_.client_order_id).exchange_order_id, held)
    fills, _ = capped.fills_since(None)
    h.executor.ingest([f for f in fills if f.action == "sell"], now=h.at(16))
    assert capped.submit(sell(17, "again")).reason == "local:venue_position_insufficient"


def test_buys_stop_when_limits_change_after_activation(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    limits, limits_hash = h.ledger.current_limits()
    bound = h.make_gateway(risk_limits_hash=limits_hash)
    bound.positions(), bound.balance()
    h.ledger.record_limits(
        replace(limits, max_order_cost=limits.max_order_cost * 2), "operator", at(5)
    )
    _, order = approve(h.ledger, make_intent(t=6, rationale="after"), "2", t=6)
    assert bound.submit(order).reason == "local:risk_limits_changed_since_activation"


def test_strays_keep_startup_unreconciled(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    h.double.lag = True
    h.double.inject("POST", "/trade-api/v2/portfolio/events/orders", "timeout_after")
    place(h, 10, tif="gtc")
    h.double.lag = False
    h.executor.reconcile(now=h.at(45))
    h.executor.reconcile(now=h.at(45 + ABSENCE_GRACE_SECONDS))  # absent twice: not found
    h.double.publish()  # an unexplained resting order appears at the venue
    fresh = h.make_gateway()
    with pytest.raises(ReconciliationMismatch):
        fresh.positions()
    fresh.balance()
    assert fresh.startup_reconciled is False
    _, sell = approve(h.ledger, make_intent(t=50, action="sell", qty="1", rationale="s"), "1")
    assert fresh.submit(sell).reason == "local:startup_reconciliation_pending"


def test_activation_binds_the_ledger_and_ignores_tls_environment(tmp_path, monkeypatch):
    setup = Setup(tmp_path)
    setup.write()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "attacker-ca.pem"))  # does not exist
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "attacker-certs"))
    real = setup.activate(transport=None)  # constructed with the bundled CA set; no request
    assert type(real.client._http._transport) is httpx.HTTPTransport
    meta = setup.ledger.meta()
    assert meta["kalshi_environment"] == "demo" and meta["kalshi_key_id"] == KEY_ID
    setup.write(environment='"production"')
    assert "ledger_binding" in setup.refused().failed


def test_resent_gateway_events_are_idempotent_and_fees_are_bounded(tmp_path):
    h = Harness(tmp_path / "l.sqlite3")
    placement = place(h, 10, tif="gtc")
    order_id = placement.submit.exchange_order_id
    h.double.execute(order_id, "2", fee="0.01")
    fills, _ = h.gateway.fills_since(None)
    (fill,) = fills
    pricey = replace(fill, fill_id=fill.fill_id + "-x", fee=D("5.00"))
    with pytest.raises(Exception, match="fee bound"):
        h.executor.ingest([pricey], now=h.at(11))
    assert "ingest_conflict" in h.executor.unknown_reasons
    assert all(f.fill_id != pricey.fill_id for f in h.ledger.fills())
    assert TICKER == fill.ticker
    # A venue event resent later (websocket replay) is the same event, not a conflict.
    coid = placement.order.client_order_id
    event = GatewayEvent("venue-ev-1", coid, "accepted", exchange_order_id=order_id)
    h.executor.ingest([event], now=h.at(12))
    h.executor.ingest([event], now=h.at(13))
    assert [e.event_id for e in h.ledger.events(coid)].count("venue-ev-1") == 1
    changed = replace(event, exchange_order_id="different")
    with pytest.raises(Exception, match="resent with different content"):
        h.executor.ingest([changed], now=h.at(14))
