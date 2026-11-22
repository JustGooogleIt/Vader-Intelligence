"""Strategy intents, orders, order events, fills and the order state machine.

An order's state is a pure function of the *set* of its events and fills: records with
the same id collapse (conflicting duplicates raise), delivery order is irrelevant, and
a terminal order is never reopened by a stale event. Precedence, highest first:

1. cumulative distinct fills reach the order quantity -> `filled`;
2. a terminal event (`rejected`, `canceled`, `expired`, or a `reconciled` venue status
   that is terminal; `not_found` counts as `rejected`) -> that state, the first such
   event in gateway order (sequence, else exchange time, receipt time, event id);
3. `submit_unknown` without any `reconciled` event -> `unknown` (only reconciliation
   resolves it, never a later acknowledgement);
4. `accepted`, any fill, or a reconciled `open`/`filled` -> `open`;
5. `submit_requested` -> `pending_submit`; otherwise `new`.

A `reconciled` event with status `absent` records a single negative venue read that is
not final yet (`executor.reconcile` confirms it later as `not_found`, or the order is
found). It resolves nothing: the order is projected as if it did not exist, so an
unknown submission stays `unknown` and an unresolved one stays without an outcome.

Fills after a cancel request or a cancel are accepted (cancel race); the final state
then reflects the remaining quantity. Fills above the order quantity, and `amended`
events (unsupported in v1), mark the state `inconsistent`.
"""

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from .fees import LIQUIDITY
from .instruments import identifier
from .money import (
    ONE,
    ZERO,
    canonical,
    digest,
    frozen_mapping,
    in_exact,
    nonnegative,
    on_grid,
    opt_dec,
    opt_utc,
    plain,
    positive,
    price,
    utc,
)

SIDES = ("yes", "no")
ACTIONS = ("buy", "sell")
TIME_IN_FORCE = ("ioc", "gtc")
LOCAL_KINDS = ("submit_requested", "cancel_requested", "submit_unknown", "canceled")
GATEWAY_KINDS = (
    "submitted",
    "accepted",
    "rejected",
    "fill",
    "canceled",
    "expired",
    "amended",
    "reconciled",
)
EVENT_KINDS = tuple(sorted(set(LOCAL_KINDS) | set(GATEWAY_KINDS)))
EVENT_SOURCES = ("local", "simulated", "exchange")
FILL_SOURCES = ("simulated", "exchange")
GATEWAY_SOURCE = {"paper": "simulated", "live": "exchange"}
ENVIRONMENTS = ("paper", "live")
TERMINAL = ("filled", "canceled", "expired", "rejected")
ACTIVE = ("new", "pending_submit", "open", "unknown")
VENUE_STATUSES = ("pending", "open", "filled", "canceled", "expired", "rejected", "not_found")
# A negative venue read that is not final yet (recorded by reconciliation, never reported
# by a gateway); see the module docstring.
ABSENT = "absent"
RECONCILED_STATUSES = (*VENUE_STATUSES, ABSENT)
_OUTCOME_KINDS = {
    "submitted",
    "accepted",
    "rejected",
    "submit_unknown",
    "reconciled",
    "canceled",
    "expired",
}


def opposite(side):
    return {"yes": "no", "no": "yes"}[side]


def _choice(value, allowed, name):
    if value not in allowed:
        raise ValueError(f"{name}: {value!r} not in {allowed}")
    return value


def _text(value, name, limit=500):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name}: expected text of at most {limit} characters")
    return value


@dataclass(frozen=True)
class OrderIntent:
    """Strategy output. Immutable; `intent_id` is the digest of every other field."""

    strategy_id: str
    forecast_ref: object
    ticker: str
    event_ticker: str
    side: str
    action: str
    limit_price: object
    max_quantity: object
    time_in_force: str
    post_only: bool
    created_at: datetime
    rationale: str = ""
    intent_id: str = ""

    def __post_init__(self):
        identifier(self.strategy_id, "strategy_id")
        identifier(self.ticker, "ticker")
        identifier(self.event_ticker, "event_ticker")
        _choice(self.side, SIDES, "side")
        _choice(self.action, ACTIONS, "action")
        _choice(self.time_in_force, TIME_IN_FORCE, "time_in_force")
        if not isinstance(self.post_only, bool):
            raise TypeError("post_only must be a bool")
        if self.post_only and self.time_in_force == "ioc":
            raise ValueError("post_only orders cannot be immediate-or-cancel")
        quantity = opt_dec(self.max_quantity, "max_quantity")
        if quantity is not None and quantity <= 0:
            raise ValueError("max_quantity must be positive when given")
        object.__setattr__(self, "forecast_ref", frozen_mapping(self.forecast_ref, "forecast_ref"))
        object.__setattr__(self, "limit_price", price(self.limit_price, "limit_price"))
        object.__setattr__(self, "max_quantity", quantity)
        object.__setattr__(self, "created_at", utc(self.created_at, "created_at"))
        _text(self.rationale, "rationale")
        expected = digest(self.content())
        if self.intent_id and self.intent_id != expected:
            raise ValueError("intent_id does not match the intent content")
        object.__setattr__(self, "intent_id", expected)

    def content(self):
        data = plain(self)
        data.pop("intent_id")
        return data

    def to_dict(self):
        return plain(self)


def client_order_id(environment, approval_id, n=1):
    """Durable client order id, fixed before any submission."""
    _choice(environment, ENVIRONMENTS, "environment")
    return f"vi-{environment}-{approval_id[:16]}-{n}"


@dataclass(frozen=True)
class Order:
    """An order created from exactly one approval. `fee_bound_per_step` fixes its
    reservation: cash for buys = remaining x limit + lots x bound."""

    client_order_id: str
    approval_id: str
    intent_id: str
    ticker: str
    side: str
    action: str
    quantity: object
    limit_price: object
    time_in_force: str
    post_only: bool
    qty_step: object
    fee_bound_per_step: object
    created_at: datetime

    def __post_init__(self):
        for name in ("client_order_id", "approval_id", "intent_id", "ticker"):
            identifier(getattr(self, name), name)
        _choice(self.side, SIDES, "side")
        _choice(self.action, ACTIONS, "action")
        _choice(self.time_in_force, TIME_IN_FORCE, "time_in_force")
        if not isinstance(self.post_only, bool):
            raise TypeError("post_only must be a bool")
        step = positive(self.qty_step, "qty_step")
        quantity = positive(self.quantity, "quantity")
        if not on_grid(quantity, step):
            raise ValueError("quantity must lie on the quantity step grid")
        limit = price(self.limit_price, "limit_price")
        if not ZERO < limit < ONE:
            raise ValueError("limit_price must be strictly between 0 and 1")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "qty_step", step)
        object.__setattr__(self, "limit_price", limit)
        object.__setattr__(
            self, "fee_bound_per_step", nonnegative(self.fee_bound_per_step, "fee_bound")
        )
        object.__setattr__(self, "created_at", utc(self.created_at, "created_at"))

    def to_dict(self):
        return plain(self)


@dataclass(frozen=True)
class OrderEvent:
    """Append-only order event. `local` events are written by the executor itself."""

    event_id: str
    client_order_id: str
    kind: str
    source: str
    received_at: datetime
    exchange_order_id: str | None = None
    sequence: int | None = None
    exchange_ts: datetime | None = None
    payload: object = field(default_factory=dict)

    def __post_init__(self):
        identifier(self.client_order_id, "client_order_id")
        _text(self.event_id, "event_id", 200)
        if not self.event_id:
            raise ValueError("event_id is required")
        _choice(self.kind, EVENT_KINDS, "kind")
        _choice(self.source, EVENT_SOURCES, "source")
        allowed = LOCAL_KINDS if self.source == "local" else GATEWAY_KINDS
        if self.kind not in allowed:
            raise ValueError(f"{self.source} source cannot record {self.kind!r}")
        if self.sequence is not None and (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("sequence must be a nonnegative integer")
        if self.exchange_order_id is not None:
            _text(self.exchange_order_id, "exchange_order_id", 200)
        payload = frozen_mapping(self.payload, "payload")
        if self.kind == "reconciled":
            _choice(payload.get("status"), RECONCILED_STATUSES, "reconciled status")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "received_at", utc(self.received_at, "received_at"))
        object.__setattr__(self, "exchange_ts", opt_utc(self.exchange_ts, "exchange_ts"))

    def to_dict(self):
        return plain(self)


@dataclass(frozen=True)
class Fill:
    fill_id: str
    client_order_id: str
    ticker: str
    side: str
    action: str
    price: object
    quantity: object
    fee: object
    liquidity: str
    exchange_ts: datetime
    source: str

    def __post_init__(self):
        _text(self.fill_id, "fill_id", 200)
        if not self.fill_id:
            raise ValueError("fill_id is required")
        identifier(self.client_order_id, "client_order_id")
        identifier(self.ticker, "ticker")
        _choice(self.side, SIDES, "side")
        _choice(self.action, ACTIONS, "action")
        _choice(self.liquidity, LIQUIDITY, "liquidity")
        _choice(self.source, FILL_SOURCES, "source")
        object.__setattr__(self, "price", price(self.price))
        object.__setattr__(self, "quantity", positive(self.quantity, "quantity"))
        object.__setattr__(self, "fee", nonnegative(self.fee, "fee"))
        object.__setattr__(self, "exchange_ts", utc(self.exchange_ts, "exchange_ts"))

    def to_dict(self):
        return plain(self)


@dataclass(frozen=True)
class OrderState:
    order: Order
    status: str
    filled_quantity: object
    fill_ids: tuple
    exchange_order_id: str | None
    cancel_requested: bool
    submit_requested: bool
    submit_outcome: bool
    accepted_at: datetime | None
    inconsistent: bool
    reason: str | None
    venue_status: str | None

    @property
    def active(self):
        return self.status in ACTIVE

    @property
    def remaining(self):
        """Quantity still able to fill; zero for terminal orders."""
        if not self.active:
            return ZERO
        return max(ZERO, self.order.quantity - self.filled_quantity)


def event_key(event):
    """Gateway order: sequence when present, else exchange time, receipt time, id."""
    return (
        event.sequence is None,
        event.sequence if event.sequence is not None else 0,
        event.exchange_ts or event.received_at,
        event.received_at,
        event.event_id,
    )


def _unique(records, key):
    seen = {}
    for record in records:
        k = getattr(record, key)
        if k in seen and canonical(seen[k]) != canonical(record):
            raise ValueError(f"conflicting records share {key} {k!r}")
        seen[k] = record
    return list(seen.values())


def _terminal(event):
    if event.kind in ("rejected", "canceled", "expired"):
        return event.kind
    if event.kind == "reconciled":
        status = event.payload["status"]
        if status in ("canceled", "expired", "rejected"):
            return status
        if status == "not_found":
            return "rejected"
    return None


@in_exact
def order_state(order, events=(), fills=()):
    """Project one order from its events and fills (order-insensitive, idempotent)."""
    events = sorted(_unique(events, "event_id"), key=event_key)
    fills = _unique(fills, "fill_id")
    for record in [*events, *fills]:
        if record.client_order_id != order.client_order_id:
            raise ValueError("record belongs to another order")
    filled = sum((f.quantity for f in fills), ZERO)
    exchange_ids = [e.exchange_order_id for e in events if e.exchange_order_id]
    # A single negative venue read (`absent`) resolves nothing.
    events = [e for e in events if not (e.kind == "reconciled" and e.payload["status"] == ABSENT)]
    kinds = {e.kind for e in events}
    reconciled = [e for e in events if e.kind == "reconciled"]
    venue = reconciled[-1].payload["status"] if reconciled else None
    terminal = [e for e in events if _terminal(e)]
    reason = None
    if filled >= order.quantity:
        status = "filled"
    elif terminal:
        status = _terminal(terminal[0])
        reason = terminal[0].payload.get("reason") or (
            "not_found_at_venue" if terminal[0].kind == "reconciled" else None
        )
    elif "submit_unknown" in kinds and not reconciled:
        status = "unknown"
    elif "accepted" in kinds or fills or venue in ("open", "filled"):
        status = "open"
    elif "submit_requested" in kinds:
        status = "pending_submit"
    else:
        status = "new"
    accepted = [
        e.exchange_ts or e.received_at
        for e in events
        if e.kind == "accepted" or (e.kind == "reconciled" and venue in ("open", "filled"))
    ] + [f.exchange_ts for f in fills]
    return OrderState(
        order=order,
        status=status,
        filled_quantity=filled,
        fill_ids=tuple(sorted(f.fill_id for f in fills)),
        exchange_order_id=exchange_ids[0] if exchange_ids else None,
        cancel_requested="cancel_requested" in kinds,
        submit_requested="submit_requested" in kinds,
        submit_outcome=bool(kinds & _OUTCOME_KINDS) or bool(fills),
        accepted_at=min(accepted) if accepted else None,
        inconsistent=filled > order.quantity or "amended" in kinds,
        reason=reason,
        venue_status=venue,
    )


def intent_from_dict(data):
    return OrderIntent(**data)


def order_from_dict(data):
    return Order(**data)


def event_from_dict(data):
    return OrderEvent(**{**data, "payload": MappingProxyType(dict(data.get("payload") or {}))})


def fill_from_dict(data):
    return Fill(**data)
