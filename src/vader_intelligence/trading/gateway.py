"""The exchange gateway protocol and the records a gateway returns.

The executor is the only caller of a gateway. A gateway never writes the ledger: it
reports results and events, and the executor records them. Exceptions raised by
`submit` (timeouts, connection loss, adapter errors) are recorded as `submit_unknown`
and resolved only by `lookup` during reconciliation; a submit is never retried.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .money import frozen_mapping, nonnegative, opt_utc
from .orders import GATEWAY_KINDS, VENUE_STATUSES

SUBMIT_STATUSES = ("submitted", "accepted", "rejected", "unknown")
CANCEL_STATUSES = ("canceled", "pending", "rejected")


@dataclass(frozen=True)
class SubmitResult:
    """`submitted`: transmitted, awaiting venue events; `accepted`/`rejected`: venue
    decision; `unknown`: outcome not known (resolved only by reconciliation)."""

    status: str
    exchange_order_id: str | None = None
    reason: str | None = None
    exchange_ts: datetime | None = None
    sequence: int | None = None

    def __post_init__(self):
        if self.status not in SUBMIT_STATUSES:
            raise ValueError(f"unknown submit status {self.status!r}")
        object.__setattr__(self, "exchange_ts", opt_utc(self.exchange_ts, "exchange_ts"))


@dataclass(frozen=True)
class CancelResult:
    status: str
    reason: str | None = None
    exchange_ts: datetime | None = None
    sequence: int | None = None

    def __post_init__(self):
        if self.status not in CANCEL_STATUSES:
            raise ValueError(f"unknown cancel status {self.status!r}")
        object.__setattr__(self, "exchange_ts", opt_utc(self.exchange_ts, "exchange_ts"))


@dataclass(frozen=True)
class OrderStatus:
    """Venue view of one order, found by client order id."""

    client_order_id: str
    status: str
    filled_quantity: object
    exchange_order_id: str | None = None

    def __post_init__(self):
        if self.status not in VENUE_STATUSES or self.status == "not_found":
            raise ValueError(f"invalid venue status {self.status!r}")
        object.__setattr__(
            self, "filled_quantity", nonnegative(self.filled_quantity, "filled_quantity")
        )


@dataclass(frozen=True)
class GatewayEvent:
    """A venue-originated order event (acceptance, cancel, expiry, rejection...)."""

    event_id: str
    client_order_id: str
    kind: str
    exchange_order_id: str | None = None
    sequence: int | None = None
    exchange_ts: datetime | None = None
    payload: object = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in GATEWAY_KINDS or self.kind in ("fill", "reconciled"):
            raise ValueError(f"gateways report {self.kind!r} through other channels")
        object.__setattr__(self, "payload", frozen_mapping(self.payload, "payload"))
        object.__setattr__(self, "exchange_ts", opt_utc(self.exchange_ts, "exchange_ts"))


class ExchangeGateway(Protocol):
    """Contract for adapters (the live adapter must honour every point):

    - `submit` returns or raises; any exception means the outcome is unknown.
    - `lookup(client_order_id)` returns None ONLY when the venue definitively has no
      order with that client id (None becomes a terminal `not_found`, which releases the
      reservation); while that is not yet certain it must raise.
    - `cancel` reports `canceled` only when the order's final filled quantity is known
      and its fills are deliverable through `fills_since` (a terminal state releases the
      remaining reservation).
    - `fills_since(cursor)` returns `(fills, next_cursor)` in venue order with stable
      fill ids; fill quantities lie on the order's quantity step.
    - `positions()` maps ticker to a holding with `yes_qty`/`no_qty`; `balance()` is
      total cash comparable with the ledger's projected cash, or None when unknown.
    """

    environment: str  # "paper" | "live"

    def submit(self, order) -> SubmitResult: ...

    def cancel(self, client_order_id: str) -> CancelResult: ...

    def lookup(self, client_order_id: str) -> OrderStatus | None: ...

    def open_orders(self) -> list: ...

    def fills_since(self, cursor: str | None) -> tuple: ...

    def positions(self) -> dict: ...

    def balance(self): ...
