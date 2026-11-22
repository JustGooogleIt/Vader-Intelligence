"""SYNTHETIC fixtures for trading tests. Tickers, books and probabilities are invented;
nothing here is market evidence or a statement about real Kalshi markets."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from vader_intelligence.trading.gateway import CancelResult, OrderStatus, SubmitResult
from vader_intelligence.trading.instruments import Market
from vader_intelligence.trading.ledger import Ledger, build_order
from vader_intelligence.trading.marketdata import BookSnapshot
from vader_intelligence.trading.orders import Fill, OrderEvent, OrderIntent
from vader_intelligence.trading.portfolio import CashEvent, replay
from vader_intelligence.trading.risk import SIMULATION_DEFAULTS, Approval

T0 = datetime(2026, 10, 19, 22, 0, tzinfo=timezone.utc)
EVENT = "SYN-26OCT19AAABBB"
TICKER = EVENT + "-AAA"
OTHER = EVENT + "-BBB"


def D(value):
    return Decimal(str(value))


def at(seconds=0):
    return T0 + timedelta(seconds=seconds)


def market(ticker=TICKER, event=EVENT, **kw):
    return Market(ticker, event, **kw)


def book(
    ticker=TICKER,
    t=0,
    yes=(("0.44", 200), ("0.43", 300)),
    no=(("0.54", 150), ("0.53", 400)),
    ref=None,
):
    return BookSnapshot(
        ticker,
        at(t),
        tuple((D(p), D(q)) for p, q in yes),
        tuple((D(p), D(q)) for p, q in no),
        "synthetic",
        ref or f"synthetic:{ticker}:{t}",
    )


def open_ledger(path, environment="paper", cash="1000", limits=SIMULATION_DEFAULTS):
    ledger = Ledger(path, environment)
    if environment == "paper" and cash is not None:
        ledger.append_cash_event(CashEvent("deposit-0", "deposit", D(cash), at(-60)))
    if environment == "live" and cash is not None:
        ledger.append_cash_event(CashEvent("adjust-0", "adjustment", D(cash), at(-60)))
    if limits is not None:
        ledger.record_limits(limits, "test-operator", at(-60))
    return ledger


def make_intent(
    ticker=TICKER,
    side="yes",
    action="buy",
    price="0.46",
    qty=None,
    tif="ioc",
    post_only=False,
    t=0,
    probability="0.55",
    event=EVENT,
    rationale="synthetic",
):
    return OrderIntent(
        strategy_id="synthetic-strategy",
        forecast_ref={"probability": probability, "model_id": "synthetic-model"},
        ticker=ticker,
        event_ticker=event,
        side=side,
        action=action,
        limit_price=D(price),
        max_quantity=None if qty is None else D(qty),
        time_in_force=tif,
        post_only=post_only,
        created_at=at(t),
        rationale=rationale,
    )


def approve(ledger, intent, quantity, t=0, fee_bound="0.02", step="1"):
    """Record intent + a hand-built approval + its order (lifecycle tests only)."""
    _, version = ledger.current_limits()
    approval = Approval(
        intent.intent_id,
        D(quantity),
        intent.limit_price,
        D(step),
        D(fee_bound),
        version,
        "synthetic-state",
        ({"check": "synthetic", "value": None, "limit": None, "passed": True},),
        {"method": "synthetic"},
        at(t),
    )
    ledger.record_intent(intent)
    ledger.record_decision(approval)
    order = build_order(ledger.environment, approval, intent, at(t))
    ledger.create_order(order)
    return approval, order


def source(ledger):
    return "simulated" if ledger.environment == "paper" else "exchange"


def submit_requested(order, t=0):
    coid = order.client_order_id
    return OrderEvent(f"{coid}:submit_requested", coid, "submit_requested", "local", at(t))


def gateway_event(order, kind, t=0, src="simulated", event_id=None, sequence=None, **payload):
    coid = order.client_order_id
    return OrderEvent(
        event_id or f"{coid}:{kind}:{t}",
        coid,
        kind,
        src,
        at(t),
        sequence=sequence,
        exchange_ts=at(t),
        payload=payload,
    )


def fill(order, qty, price=None, t=0, fee="0", fill_id=None, src="simulated", liquidity="taker"):
    return Fill(
        fill_id or f"{order.client_order_id}:fill:{t}:{qty}",
        order.client_order_id,
        order.ticker,
        order.side,
        order.action,
        D(price or order.limit_price),
        D(qty),
        D(fee),
        liquidity,
        at(t),
        src,
    )


class FakeVenue:
    """Deterministic SYNTHETIC exchange double. Its state survives executor restarts
    (it is the venue). Failure modes are switched on by the test."""

    def __init__(self, environment="paper", cash="1000"):
        self.environment = environment
        self.source = "simulated" if environment == "paper" else "exchange"
        self.cash = D(cash)
        self.orders = {}
        self.fill_log = []
        self.submit_calls = []
        self.fail_submit = None  # None | "before" | "after"
        self.lookup_error = False

    def submit(self, order):
        self.submit_calls.append(order.client_order_id)
        if self.fail_submit == "before":
            raise TimeoutError("synthetic timeout before the venue saw the order")
        self.orders[order.client_order_id] = {"order": order, "status": "open", "filled": D(0)}
        if self.fail_submit == "after":
            raise TimeoutError("synthetic timeout after the venue accepted the order")
        return SubmitResult("accepted", exchange_order_id="x-" + order.client_order_id[-20:])

    def execute(self, coid, qty, price=None, t=0, fee="0"):
        entry = self.orders[coid]
        f = fill(
            entry["order"], qty, price, t, fee, f"venue-fill-{len(self.fill_log)}", self.source
        )
        self.fill_log.append(f)
        entry["filled"] += D(qty)
        if entry["filled"] == entry["order"].quantity:
            entry["status"] = "filled"
        return f

    def cancel(self, coid):
        entry = self.orders.get(coid)
        if entry is None or entry["status"] != "open":
            return CancelResult("rejected", reason="not_open")
        entry["status"] = "canceled"
        return CancelResult("canceled", reason="user")

    def lookup(self, coid):
        if self.lookup_error:
            raise ConnectionError("synthetic lookup failure")
        entry = self.orders.get(coid)
        if entry is None:
            return None
        return OrderStatus(coid, entry["status"], entry["filled"])

    def open_orders(self):
        return [self.lookup(c) for c, e in sorted(self.orders.items()) if e["status"] == "open"]

    def fills_since(self, cursor):
        start = int(cursor or 0)
        return self.fill_log[start:], (str(len(self.fill_log)) if self.fill_log else cursor)

    def _replay(self):
        kind = "deposit" if self.environment == "paper" else "adjustment"
        return replay(self.fill_log, (), [CashEvent("venue", kind, self.cash, T0)])

    def positions(self):
        return dict(self._replay().holdings)

    def balance(self):
        return self._replay().cash
