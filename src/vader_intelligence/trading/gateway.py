"""The exchange gateway protocol and the records a gateway returns.

The executor is the only caller of a gateway. A gateway never writes the ledger: it
reports results and events, and the executor records them. Exceptions raised by
`submit` (timeouts, connection loss, adapter errors) are recorded as `submit_unknown`
and resolved only by `lookup` during reconciliation; a submit is never retried.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .money import digest, frozen_mapping, nonnegative, opt_utc, price, utc
from .orders import GATEWAY_KINDS, VENUE_STATUSES
from .portfolio import RESULTS

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


@dataclass(frozen=True)
class VenueSettlement:
    """A venue's settlement of one market for this account: the result, the YES payout
    per contract, the YES/NO counts it settled, the cash it paid and any fee charged."""

    ticker: str
    result: str
    settlement_value: object
    yes_count: object
    no_count: object
    revenue: object
    fee: object
    settled_at: datetime
    source_ref: str

    def __post_init__(self):
        if not isinstance(self.ticker, str) or not self.ticker:
            raise ValueError("ticker is required")
        if self.result not in RESULTS:
            raise ValueError(f"unknown settlement result {self.result!r}")
        if not isinstance(self.source_ref, str) or not self.source_ref:
            raise ValueError("source_ref is required")
        object.__setattr__(
            self, "settlement_value", price(self.settlement_value, "settlement_value")
        )
        for name in ("yes_count", "no_count", "revenue", "fee"):
            object.__setattr__(self, name, nonnegative(getattr(self, name), name))
        object.__setattr__(self, "settled_at", utc(self.settled_at, "settled_at"))


# Fingerprint schemes: `vs1` digests every field of a parsed `VenueSettlement`; `raw1`
# digests a venue record as listed (for records that do not parse). A new canonical
# form gets a new scheme, so earlier acknowledgements never match by accident.
SETTLEMENT_FINGERPRINT_SCHEME = "vs1"
RAW_SETTLEMENT_FINGERPRINT_SCHEME = "raw1"


def settlement_fingerprint(record):
    """Content fingerprint of a parsed venue settlement record: a digest of every field
    (ticker, result, value, counts, revenue, fee, settled time, source reference), so a
    record whose content changes under the same source timestamp is a different record.
    Operators acknowledge records by these fingerprints (`executor.acknowledge_settlement`)."""
    if not isinstance(record, VenueSettlement):
        raise TypeError("a settlement fingerprint needs a VenueSettlement")
    return f"{SETTLEMENT_FINGERPRINT_SCHEME}:{digest(record)}"


def raw_settlement_fingerprint(data):
    """Content fingerprint of a venue settlement record as listed (any JSON structure),
    for a record that cannot be parsed."""
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return f"{RAW_SETTLEMENT_FINGERPRINT_SCHEME}:{hashlib.sha256(text.encode()).hexdigest()}"


@dataclass(frozen=True)
class UnreadableSettlement:
    """A venue settlement record that could not be mapped. `token` names it (its ticker
    when that is readable) for reasons and operator acknowledgements. `records` holds
    the distinct parsed settlements the venue lists for that ticker when it lists several
    (`conflicting_records`), or alongside an unparseable one; it is empty for a lone
    unmappable record. `fingerprints` names every distinct venue record behind the token
    (parsed ones by `settlement_fingerprint`, unparseable ones by
    `raw_settlement_fingerprint`): that is what an operator acknowledges. A gateway that
    gives none gets them derived from `records`; with neither, nothing can be resolved."""

    token: str
    error: str
    records: tuple = ()
    fingerprints: tuple = ()

    def __post_init__(self):
        if not self.fingerprints and self.records:
            derived = tuple(settlement_fingerprint(r) for r in self.records)
            object.__setattr__(self, "fingerprints", derived)


class ExchangeGateway(Protocol):
    """Contract for adapters (the live adapter must honour every point):

    - `submit` returns or raises; any exception means the outcome is unknown.
    - `lookup(client_order_id)` returns None ONLY when the venue definitively has no
      order with that client id (None becomes a terminal `not_found`, which releases the
      reservation); while that is not yet certain it must raise. For an order whose
      submission outcome is unknown the executor still takes None as final only when a
      second None arrives after a grace period and `open_orders()` does not list it.
    - `open_orders()` lists the venue's open orders as `OrderStatus`es; reconciliation
      flags any that is not an active ledger order (`venue_open_untracked`).
    - `cancel` reports `canceled` only when the order's final filled quantity is known
      and its fills are deliverable through `fills_since` (a terminal state releases the
      remaining reservation).
    - `fills_since(cursor)` returns `(fills, next_cursor)` in venue order with stable
      fill ids; fill quantities lie on the order's quantity step.
    - `positions()` maps ticker to a holding with `yes_qty`/`no_qty`; `balance()` is
      total cash comparable with the ledger's projected cash, or None when unknown.
    - Optional: `settlements_since(cursor)` returns `(records, next_cursor)`, each record a
      `VenueSettlement` or an `UnreadableSettlement`, for gateways whose venue settles
      markets (the live adapter). Gateways without it (the simulator, the dry run) leave
      settlements to their caller.
    """

    environment: str  # "paper" | "live"

    def submit(self, order) -> SubmitResult: ...

    def cancel(self, client_order_id: str) -> CancelResult: ...

    def lookup(self, client_order_id: str) -> OrderStatus | None: ...

    def open_orders(self) -> list: ...

    def fills_since(self, cursor: str | None) -> tuple: ...

    def positions(self) -> dict: ...

    def balance(self): ...
