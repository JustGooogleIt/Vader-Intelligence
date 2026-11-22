"""Dry run: the exact live requests are constructed (and signed with a throwaway key)
and recorded; no transport is ever called. Paired only with a paper ledger."""

import base64
import json

import httpx
import pytest
from _trading_synthetic import TICKER, D, at, book, make_intent, market, open_ledger
from cryptography.hazmat.primitives.asymmetric import ed25519

from vader_intelligence.trading.executor import Executor
from vader_intelligence.trading.fees import KALSHI_2026_02_GENERAL as FEES
from vader_intelligence.trading.kalshi.auth import Signer
from vader_intelligence.trading.kalshi.dryrun import DryRunGateway
from vader_intelligence.trading.kalshi.gateway import MarketRule
from vader_intelligence.trading.kalshi.mapping import price_ranges
from vader_intelligence.trading.ledger import EnvironmentMismatch
from vader_intelligence.trading.risk import Approval, ForecastStatus, RiskEngine

from ._kalshi_double import KEY_ID, LINEAR_CENT, SYN_RULE, FakeClock, live_ledger


@pytest.fixture
def transport_calls(monkeypatch):
    """Every way httpx could send is replaced by a recorder that also fails."""
    calls = []

    def forbidden(self, *args, **kwargs):
        calls.append(type(self).__name__)
        raise AssertionError("the dry run must not send anything")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(httpx.MockTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    return calls


def place(executor, t=10):
    return executor.place(
        make_intent(t=t, tif="gtc"),
        now=at(t),
        market=market(),
        snapshot=book(t=t - 5),
        forecast=ForecastStatus(TICKER, D("0.55"), at(t - 5), "synthetic-model"),
        marks={TICKER: book(t=t - 5)},
    )


def test_dry_run_constructs_signs_and_records_but_sends_nothing(tmp_path, transport_calls):
    clock = FakeClock(at(10))
    throwaway = ed25519.Ed25519PrivateKey.generate()  # never a real credential
    gateway = DryRunGateway(
        "demo",
        markets=(SYN_RULE,),
        cash=D(1000),
        signer=Signer(KEY_ID, throwaway, clock_ms=clock.ms),
        now=clock.now,
    )
    with open_ledger(tmp_path / "paper.sqlite3") as ledger:
        executor = Executor(ledger, gateway, RiskEngine("paper"), FEES)
        placement = place(executor)
        assert isinstance(placement.decision, Approval)
        assert placement.submit.status == "submitted"
        coid = placement.order.client_order_id
        (create,) = gateway.requests
        assert create.method == "POST"
        assert (
            create.url == "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/events/orders"
        )
        assert json.loads(create.body) == {
            "ticker": TICKER,
            "side": "bid",
            "price": "0.4600",
            "count": f"{placement.order.quantity}.00",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": coid,
            "post_only": False,
            "reduce_only": False,
            "exchange_index": 3,
        }
        ts = create.headers["KALSHI-ACCESS-TIMESTAMP"]
        assert create.headers["KALSHI-ACCESS-KEY"] == KEY_ID and int(ts) == clock.ms()
        throwaway.public_key().verify(
            base64.b64decode(create.headers["KALSHI-ACCESS-SIGNATURE"]),
            f"{ts}POST/trade-api/v2/portfolio/events/orders".encode(),
        )
        assert executor.reconcile(now=at(11)).clean
        assert ledger.projection().orders[coid].status == "open"  # never fills
        assert executor.cancel(coid, now=at(12)).status == "canceled"
        cancel = gateway.requests[-1]
        assert cancel.method == "DELETE" and cancel.params == {"market_ticker": TICKER}
        assert cancel.url.startswith(
            "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/events/orders/"
        )
        exported = json.loads(json.dumps(gateway.records()))[0]
        assert exported["client_order_id"] == coid
        signature = create.headers["KALSHI-ACCESS-SIGNATURE"]
        assert exported["headers"]["KALSHI-ACCESS-SIGNATURE"].startswith("sha256:")
        assert signature not in json.dumps(gateway.records())  # never exported replayable
        assert ledger.verify() == [] and ledger.fills() == []
    assert transport_calls == []


def test_unsigned_dry_run_and_local_refusals(tmp_path, transport_calls):
    other = MarketRule("OTHER", price_ranges(LINEAR_CENT))
    with open_ledger(tmp_path / "paper.sqlite3") as ledger:
        unsigned = DryRunGateway("production", markets=(SYN_RULE,), cash=D(1000))
        executor = Executor(ledger, unsigned, RiskEngine("paper"), FEES)
        place(executor, 10)
        (request,) = unsigned.requests
        assert request.url.startswith("https://external-api.kalshi.com/trade-api/v2/")
        assert not any(h.startswith("KALSHI-ACCESS") for h in request.headers)
    with open_ledger(tmp_path / "other.sqlite3") as ledger:
        refusing = DryRunGateway("demo", markets=(other,), cash=D(1000))
        executor = Executor(ledger, refusing, RiskEngine("paper"), FEES)
        result = place(executor, 10).submit
        assert (result.status, result.reason) == ("rejected", "local:ticker_not_allowed")
        assert refusing.requests == [] and refusing.refusals
    assert transport_calls == []


def test_dry_run_is_a_paper_gateway(tmp_path):
    gateway = DryRunGateway("demo", markets=(SYN_RULE,), cash=D(1000))
    assert gateway.environment == "paper"
    with live_ledger(tmp_path / "live.sqlite3") as ledger:
        with pytest.raises(EnvironmentMismatch):
            Executor(ledger, gateway, RiskEngine("live"), FEES)
    with pytest.raises(ValueError):
        DryRunGateway("staging", markets=(SYN_RULE,), cash=D(1000))
