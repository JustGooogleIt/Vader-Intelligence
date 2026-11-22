"""Mapping between trading-package records and Kalshi Trade API v2 shapes.

Create body (facts §2.1 [V:OAS CreateOrderV2Request]): `ticker`, `side` (`bid` = buy
YES, `ask` = sell YES), `price` (always the YES price, dollars string), `count`
(fixed-point string), `time_in_force`, `self_trade_prevention_type`, `client_order_id`,
`post_only`, `reduce_only`, optional `exchange_index`.

Our side/action at limit price p maps to a book side and a YES price:

    buy YES  at p  ->  bid at p          sell YES at p  ->  ask at p
    buy NO   at q  ->  ask at 1 - q      sell NO  at q  ->  bid at 1 - q

([V:OAS BookSide]: "Selling YES is economically equivalent to buying NO at 1 - price";
the buy-NO row is [INF] in facts §2.1, supported by [V:DIR] mapping legacy (buy, no) to
`ask`.) The inverse needs the outcome side we trade, because `ask` is both "sell YES"
and "buy NO".

- Prices: requests accept 2-4 decimals (facts §6 [V:FP]); we send exactly 4. The YES
  price must lie strictly inside (0, 1) ("$0 or $1" are rejected, [V:CL]) and on the
  market's `price_ranges` grid, "the source of truth for valid prices" ([V:FP]).
- Counts: requests accept 0-2 decimals, minimum granularity 0.01 ([V:FP]); we send 2.
- `time_in_force`: `ioc` -> `immediate_or_cancel`, `gtc` -> `good_till_canceled`
  ([V:OAS]). `self_trade_prevention_type` is always `taker_at_cross` (cancels our
  incoming order instead of trading with ourselves; partial fills already matched
  stand, [V:OAS]). `reduce_only` is set for immediate-or-cancel sells only, the one
  combination the exchange accepts ([V:OAS]).
- Exchange `Order` objects ([V:OAS Order]): status `resting|canceled|executed` (no
  `pending`, facts §2.2) map to `open|canceled|filled`; counts from `*_count_fp`;
  `yes_price_dollars`; fees from `taker_fees_dollars + maker_fees_dollars`.
- Exchange `Fill` objects (facts §2.5 [V:OAS]): `fill_id`, `order_id`, `ticker`,
  `book_side`, `count_fp`, `yes_price_dollars`, `no_price_dollars`, `is_taker`
  (True -> `taker`, False -> `maker`), `created_time` (RFC 3339), `fee_cost`.

Assumptions (UNVERIFIED; also listed in docs/live-canary-runbook.md):

- the `book_side` of a REST fill is the book side of OUR order (not the taker's); a
  fill whose book side differs from its order's is treated as a mismatch (fail closed);
- `fee_cost` is the total fee of that fill in dollars and is never negative (rounding
  rebates, facts §7 [V:FR], are not modelled per fill);
- the `outcome_side` reported for orders created through V2 is not used (its value for
  a V2 `ask` is not documented);
- a `balance_breakdown` item's `balance` is integer cents (text is read as dollars);
  the reconciled total is always the exact `balance_dollars` (facts §2.7);
- a settlement record (facts §2.8 [V:OAS]) settles this account's whole holding of its
  market: `yes_count_fp`/`no_count_fp` are the contracts held at settlement, `revenue`
  is the cash paid for them in integer cents, `fee_cost` is the settlement fee in
  dollars (zero for yes/no results, facts §7 [V:SET]) and `value`, when present, is the
  YES payout in cents; `scalar` results settle at `value`. Records with any other result
  or without these fields fail closed.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ..gateway import OrderStatus, VenueSettlement
from ..money import ONE, ZERO, dec, in_exact, on_grid
from ..orders import Fill
from .client import ORDER_ID

COUNT_STEP = Decimal("0.01")
PRICE_QUANTUM = Decimal("0.0001")
TIME_IN_FORCE = {"ioc": "immediate_or_cancel", "gtc": "good_till_canceled"}
SELF_TRADE_PREVENTION = "taker_at_cross"
BOOK_SIDES = ("bid", "ask")
EXCHANGE_STATUS = {"resting": "open", "canceled": "canceled", "executed": "filled"}
_BOOK_SIDE = {
    ("yes", "buy"): "bid",
    ("yes", "sell"): "ask",
    ("no", "buy"): "ask",
    ("no", "sell"): "bid",
}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FILL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TICKER = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_RFC3339 = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"(?:([Zz])|([+-])(\d{2}):(\d{2}))\Z"
)


class MappingError(ValueError):
    """A record that cannot be mapped faithfully. `code` is a stable reason string."""

    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


# -- prices and grids ---------------------------------------------------------------


@dataclass(frozen=True)
class PriceRange:
    """One `price_ranges` entry: valid prices start, start + step, ... up to end."""

    start: object
    end: object
    step: object

    def __post_init__(self):
        start, end, step = (dec(getattr(self, n), n) for n in ("start", "end", "step"))
        if not (ZERO <= start < end <= ONE) or step <= 0 or step > end - start:
            raise ValueError("price range must satisfy 0 <= start < end <= 1, 0 < step")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "step", step)

    @in_exact
    def contains(self, value):
        return self.start <= value <= self.end and on_grid(value - self.start, self.step)


def price_ranges(items):
    """Tuple of `PriceRange` from `[{start, end, step}, ...]` (strings, not floats)."""
    ranges = tuple(PriceRange(i["start"], i["end"], i["step"]) for i in items)
    if not ranges:
        raise ValueError("at least one price range is required")
    return ranges


def on_price_grid(value, ranges):
    """Valid YES price: strictly inside (0, 1) and on some range's grid."""
    return ZERO < value < ONE and any(r.contains(value) for r in ranges)


def book_side(side, action):
    """Book side of our (side, action) (see module table)."""
    try:
        return _BOOK_SIDE[(side, action)]
    except KeyError:
        raise MappingError("unsupported_side_action") from None


@in_exact
def yes_price(side, value):
    """The YES price quoted for our side's price (NO at q is YES at 1 - q)."""
    if side == "yes":
        return value
    if side == "no":
        return ONE - value
    raise MappingError("unsupported_side")


def side_action(book, outcome_side):
    """Inverse of `book_side` for a known outcome side we trade."""
    for action in ("buy", "sell"):
        if book_side(outcome_side, action) == book:
            return outcome_side, action
    raise MappingError("unsupported_book_side")


def side_price(side, yes):
    """Our side's price from a YES price (inverse of `yes_price`)."""
    return yes_price(side, yes)  # the map p -> 1 - p is its own inverse


def _exact_text(value, quantum):
    quantized = value.quantize(quantum)
    if quantized != value:
        raise MappingError("precision", "value has more decimals than the wire format allows")
    return format(quantized, "f")


@dataclass(frozen=True)
class OrderRequest:
    """A V2 create-order body as data."""

    ticker: str
    side: str
    price: object
    count: object
    time_in_force: str
    client_order_id: str
    post_only: bool
    reduce_only: bool
    exchange_index: int | None = None
    self_trade_prevention_type: str = SELF_TRADE_PREVENTION

    def body(self):
        data = {
            "ticker": self.ticker,
            "side": self.side,
            "price": _exact_text(self.price, PRICE_QUANTUM),
            "count": _exact_text(self.count, COUNT_STEP),
            "time_in_force": self.time_in_force,
            "self_trade_prevention_type": self.self_trade_prevention_type,
            "client_order_id": self.client_order_id,
            "post_only": self.post_only,
            "reduce_only": self.reduce_only,
        }
        if self.exchange_index is not None:
            data["exchange_index"] = self.exchange_index
        return data


@in_exact
def order_request(order, ranges, exchange_index=None):
    """The create body for one `Order`, or `MappingError` with a stable code."""
    if order.time_in_force not in TIME_IN_FORCE:
        raise MappingError("unsupported_time_in_force")
    if order.post_only and order.time_in_force == "ioc":
        raise MappingError("post_only_ioc")
    count = order.quantity
    if count <= 0 or not on_grid(count, COUNT_STEP):
        raise MappingError("count_off_grid", "quantity must be a positive multiple of 0.01")
    yes = yes_price(order.side, order.limit_price)
    if yes.quantize(PRICE_QUANTUM) != yes or not on_price_grid(yes, ranges):
        raise MappingError("price_off_grid", "YES price is not on the market's price grid")
    if exchange_index is not None and (
        isinstance(exchange_index, bool) or not isinstance(exchange_index, int)
    ):
        raise MappingError("exchange_index")
    return OrderRequest(
        ticker=order.ticker,
        side=book_side(order.side, order.action),
        price=yes,
        count=count,
        time_in_force=TIME_IN_FORCE[order.time_in_force],
        client_order_id=order.client_order_id,
        post_only=order.post_only,
        reduce_only=order.action == "sell" and order.time_in_force == "ioc",
        exchange_index=exchange_index,
    )


# -- exchange records ---------------------------------------------------------------


def _field(data, name):
    if not isinstance(data, dict) or name not in data or data[name] is None:
        raise MappingError("missing_field", f"exchange record lacks {name!r}")
    return data[name]


def _text_field(data, name, pattern):
    value = _field(data, name)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MappingError("invalid_field", f"exchange field {name!r} is invalid")
    return value


def _decimal(data, name, minimum=ZERO):
    value = _field(data, name)
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise MappingError("invalid_field", f"exchange field {name!r} is not fixed-point text")
    try:
        result = dec(value, name)
    except (TypeError, ValueError):
        raise MappingError("invalid_field", f"exchange field {name!r} is invalid") from None
    if minimum is not None and result < minimum:
        raise MappingError("invalid_field", f"exchange field {name!r} is out of range")
    return result


def rfc3339(value):
    """Aware UTC datetime from RFC 3339 text; fractions beyond microseconds truncate."""
    match = _RFC3339.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise MappingError("invalid_time", "timestamp is not RFC 3339")
    year, month, day, hour, minute, second, fraction, zulu, sign, oh, om = match.groups()
    micros = int((fraction or "0")[:6].ljust(6, "0"))
    offset = timedelta(0)
    if not zulu:
        offset = timedelta(hours=int(oh), minutes=int(om)) * (1 if sign == "+" else -1)
    try:
        local = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    except ValueError:
        raise MappingError("invalid_time", "timestamp is not a valid date") from None
    return (local.replace(microsecond=micros) - offset).replace(tzinfo=timezone.utc)


def from_ms(value):
    """Aware UTC datetime from integer epoch milliseconds (exact)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MappingError("invalid_time", "ts_ms must be nonnegative integer milliseconds")
    return EPOCH + timedelta(milliseconds=value)


def epoch_seconds(moment):
    """Whole seconds since the epoch, floored (for `min_ts` filters)."""
    return (moment - EPOCH) // timedelta(seconds=1)


@dataclass(frozen=True)
class VenueOrder:
    """The fields of an exchange `Order` the adapter relies on."""

    order_id: str
    client_order_id: str | None
    ticker: str
    book_side: str
    status: str
    yes_price: object
    initial_count: object
    fill_count: object
    remaining_count: object
    fees: object


def venue_order(data):
    """Strictly parsed exchange `Order` object (facts §2.4 [V:OAS Order])."""
    order_id = _text_field(data, "order_id", ORDER_ID)
    coid = data.get("client_order_id") if isinstance(data, dict) else None
    if coid is not None and (not isinstance(coid, str) or len(coid) > 200):
        raise MappingError("invalid_field", "client_order_id is invalid")
    side = _field(data, "book_side")
    if side not in BOOK_SIDES:
        raise MappingError("invalid_field", "book_side is invalid")
    status = _field(data, "status")
    if status not in EXCHANGE_STATUS:
        raise MappingError("unknown_status", f"exchange order status {status!r} is not mapped")
    yes = _decimal(data, "yes_price_dollars")
    initial = _decimal(data, "initial_count_fp")
    filled = _decimal(data, "fill_count_fp")
    remaining = _decimal(data, "remaining_count_fp")
    fees = ZERO
    for name in ("taker_fees_dollars", "maker_fees_dollars"):
        if data.get(name) is not None:
            fees += _decimal(data, name)
    if not ZERO < yes < ONE or filled > initial or remaining > initial:
        raise MappingError("inconsistent_order", "exchange order counts or price are invalid")
    if status == "executed" and (filled != initial or remaining != 0):
        raise MappingError("inconsistent_order", "executed order is not fully filled")
    return VenueOrder(
        order_id=order_id,
        client_order_id=coid or None,
        ticker=_text_field(data, "ticker", _TICKER),
        book_side=side,
        status=status,
        yes_price=yes,
        initial_count=initial,
        fill_count=filled,
        remaining_count=remaining,
        fees=fees,
    )


def order_status(venue):
    """Gateway `OrderStatus` of an exchange order (resting/canceled/executed)."""
    return OrderStatus(
        client_order_id=venue.client_order_id or "",
        status=EXCHANGE_STATUS[venue.status],
        filled_quantity=venue.fill_count,
        exchange_order_id=venue.order_id,
    )


def mismatches(order, venue):
    """Differences between our `Order` and the exchange order carrying its id."""
    problems = []
    if venue.client_order_id != order.client_order_id:
        problems.append("client_order_id")
    if venue.ticker != order.ticker:
        problems.append("ticker")
    if venue.book_side != book_side(order.side, order.action):
        problems.append("book_side")
    if venue.yes_price != yes_price(order.side, order.limit_price):
        problems.append("yes_price")
    if venue.initial_count != order.quantity:
        problems.append("initial_count")
    return problems


def fill_summary(data):
    """(fill_id, order_id, count) of a REST fill (facts §2.5; `trade_id` is the legacy
    name of the same id)."""
    fill_id = data.get("fill_id") if isinstance(data, dict) else None
    if fill_id is None and isinstance(data, dict):
        fill_id = data.get("trade_id")
    if not isinstance(fill_id, str) or not _FILL_ID.fullmatch(fill_id):
        raise MappingError("invalid_field", "fill_id is invalid")
    order_id = _text_field(data, "order_id", ORDER_ID)
    count = _decimal(data, "count_fp")
    if count <= 0 or not on_grid(count, COUNT_STEP):
        raise MappingError("invalid_field", "fill count is invalid")
    return fill_id, order_id, count


@in_exact
def fill_from_exchange(data, order):
    """Our exchange-sourced `Fill` for one REST fill of `order` (facts §2.5)."""
    fill_id, _, count = fill_summary(data)
    ticker = data.get("ticker") or data.get("market_ticker")
    if ticker != order.ticker:
        raise MappingError("fill_mismatch", "fill ticker differs from its order")
    if _field(data, "book_side") != book_side(order.side, order.action):
        raise MappingError("fill_mismatch", "fill book side differs from its order")
    yes = _decimal(data, "yes_price_dollars")
    if not ZERO < yes < ONE:
        raise MappingError("invalid_field", "fill price is invalid")
    if data.get("no_price_dollars") is not None and yes + _decimal(data, "no_price_dollars") != ONE:
        raise MappingError("fill_mismatch", "fill YES and NO prices do not sum to 1")
    taker = _field(data, "is_taker")
    if not isinstance(taker, bool):
        raise MappingError("invalid_field", "is_taker is not a boolean")
    return Fill(
        fill_id="kalshi:" + fill_id,
        client_order_id=order.client_order_id,
        ticker=order.ticker,
        side=order.side,
        action=order.action,
        price=side_price(order.side, yes),
        quantity=count,
        fee=_decimal(data, "fee_cost"),
        liquidity="taker" if taker else "maker",
        exchange_ts=rfc3339(_field(data, "created_time")),
        source="exchange",
    )


def position_from_exchange(data):
    """(ticker, signed YES position) of a `market_positions` item: positive = YES held,
    negative = NO held (facts §2.6 [V:OAS])."""
    ticker = _text_field(data, "ticker", _TICKER)
    return ticker, _decimal(data, "position_fp", minimum=None)


@in_exact
def balance_from_exchange(data):
    """(total dollars, {exchange_index: dollars}) of a balance response.

    The total is `balance_dollars`, the exact fixed-point field (facts §2.7 [V:OAS]).
    ASSUMPTION (UNVERIFIED): a `balance_breakdown` item's `balance` is integer cents
    when it is an integer (like the top-level `balance`) and dollars when it is text.
    """
    total = _decimal(data, "balance_dollars")
    shards = {}
    for item in data.get("balance_breakdown") or ():
        index = _field(item, "exchange_index")
        value = _field(item, "balance")
        if isinstance(index, bool) or not isinstance(index, int) or index in shards:
            raise MappingError("invalid_field", "balance_breakdown exchange_index is invalid")
        if isinstance(value, int) and not isinstance(value, bool):
            shards[index] = Decimal(value) / 100
        else:
            shards[index] = _decimal(item, "balance")
    return total, shards


@in_exact
def settlement_from_exchange(data):
    """`VenueSettlement` of a `/portfolio/settlements` item (facts §2.8 [V:OAS]).

    `yes` and `no` settle at 1 and 0 per YES contract; `scalar` settles at `value` cents
    (result `fair`). Any other result, a missing count, revenue or fee field, or a
    `value` that contradicts a yes/no result raises `MappingError` (fail closed).
    """
    ticker = _text_field(data, "ticker", _TICKER)
    result = _field(data, "market_result")
    value = data.get("value")
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise MappingError("invalid_field", "settlement value is not integer cents")
    if value is not None and not 0 <= value <= 100:
        raise MappingError("invalid_field", "settlement value is out of range")
    if result in ("yes", "no"):
        payout = ONE if result == "yes" else ZERO
        if value is not None and Decimal(value) / 100 != payout:
            raise MappingError("inconsistent_settlement", "settlement value contradicts result")
    elif result == "scalar" and value is not None:
        result, payout = "fair", Decimal(value) / 100
    else:
        raise MappingError("unknown_result", f"settlement result {result!r} is not mapped")
    counts = {}
    for name in ("yes_count_fp", "no_count_fp"):
        counts[name] = _decimal(data, name)
        if not on_grid(counts[name], COUNT_STEP):
            raise MappingError("invalid_field", f"settlement {name} is off the count grid")
    revenue = _field(data, "revenue")
    if isinstance(revenue, bool) or not isinstance(revenue, int) or revenue < 0:
        raise MappingError("invalid_field", "settlement revenue is not integer cents")
    settled_at = rfc3339(_field(data, "settled_time"))
    return VenueSettlement(
        ticker=ticker,
        result=result,
        settlement_value=payout,
        yes_count=counts["yes_count_fp"],
        no_count=counts["no_count_fp"],
        revenue=Decimal(revenue) / 100,
        fee=_decimal(data, "fee_cost"),
        settled_at=settled_at,
        # The parsed instant, so another spelling of the same time is the same record.
        source_ref=f"kalshi:portfolio/settlements:{ticker}:{settled_at.isoformat()}",
    )


def settlement_token(data):
    """A name for a settlement record that may not map: its ticker when readable, else
    a digest of the record."""
    ticker = data.get("ticker") if isinstance(data, dict) else None
    if isinstance(ticker, str) and _TICKER.fullmatch(ticker):
        return ticker
    text = json.dumps(data, sort_keys=True, default=str)
    return "record-" + hashlib.sha256(text.encode()).hexdigest()[:16]
