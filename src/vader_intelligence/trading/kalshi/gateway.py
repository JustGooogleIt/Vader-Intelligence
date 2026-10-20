"""`KalshiGateway`: the live `ExchangeGateway` over the allowlisted Trade API v2 endpoints.

Constructed only by `activation.activate_live` (with a network transport) or by tests
(with an `httpx.MockTransport` exchange double). It reads the live ledger (orders, kill
switch, active orders) and never writes it; the executor records everything.

How the `trading.gateway.ExchangeGateway` contract is met:

- `submit`: refuses locally, sending nothing, when the gateway is halted, the order is
  not this live ledger's, a buy meets an engaged kill switch, startup reconciliation
  (positions incl. resting orders, and balance) has not run on this instance, the
  ticker is not allowed, the price/count is off grid, a per-order or daily notional
  cap would be exceeded, or a sell is not covered by the venue position of the last
  reconciliation (net of other active sells) -> `rejected` with reason `local:<code>`.
  Otherwise one POST /portfolio/events/orders: 201 -> `accepted` with the exchange
  order id; a definitive 4xx -> `rejected` (`exchange:<code>`); 409 (duplicate
  `client_order_id`, facts §2.3 [V:QORD]) -> lookup by client order id: found ->
  `accepted`, otherwise raise. A timeout, transport error, 429, 5xx or unparseable 2xx
  raises: the executor records `submit_unknown`. Nothing is ever retried here; a resubmission, if a caller ever makes
  one, is the same `Order` (same `client_order_id`, same body), so the exchange's
  duplicate rule turns it into 409 -> lookup instead of a second order.
- `lookup(client_order_id)`: GET /portfolio/orders/{id} when this instance learnt the
  exchange id, else GET /portfolio/orders?ticker=&min_ts= over every page with
  client-side matching (there is no server filter, facts §2.3 [V:OAS]). Two venue orders
  with one client id raise (duplicate exposure); a venue order whose ticker, book side,
  YES price or initial count differs from ours raises. `None` is returned only after
  the whole window was scanned AND the order is older than the settle window AND
  younger than the live listing window AND no 201 was ever seen for it; otherwise it
  raises (not definitive).
- `cancel`: DELETE /portfolio/events/orders/{id}?market_ticker= (auto-routing needs the
  ticker, facts §12 [V:SHARD]), then GET the order and the fills of that order:
  `canceled` only when the order is canceled, its fills sum to its `fill_count_fp` (so
  every fill up to the cancel is deliverable through `fills_since`) AND the ledger
  already records that quantity (the executor releases the reservation on `canceled`;
  exposure must never be understated); otherwise `pending` (reservation kept; the next
  reconciliation records the terminal state and the fills together) or `rejected`
  (already executed / not found).
- `fills_since(cursor)`: GET /portfolio/fills?min_ts= over every page. The cursor is
  ours (`ts:<unix seconds>`, a watermark), re-read with an overlap; duplicates are
  idempotent in the ledger. Fills of orders that are not this ledger's raise.
- `positions()`: GET /portfolio/positions (signed `position_fp`, facts §2.6) plus every
  resting order: a resting venue order that is not an active ledger order (a stray or
  foreign order) raises, so reconciliation blocks risk-increasing orders.
- `balance()`: `balance_dollars` of GET /portfolio/balance (all shards); the per-shard
  breakdown is kept in `shard_balances` (MLB markets are on shard 3, facts §12).

Assumptions (UNVERIFIED or undocumented; also listed in docs/live-canary-runbook.md):

- `min_ts` on the order and fill listings is Unix seconds and filters on creation time
  or a later timestamp (facts §2.4/§2.5 say only "Unix"); lookups subtract a margin;
- REST reads lag the matching engine by an undocumented "short delay" (facts §9): a
  lookup reports "not found" only `settle_seconds` (default 30) after the order was
  created, and fills are re-read with `fills_overlap_seconds` (default 3600) overlap;
- canceled/executed orders older than the historical cutoff are served only by
  /historical/orders (facts §2.4 [V:HIST]), which is not allowlisted: lookups refuse
  to report "not found" for orders older than `max_lookup_age_seconds` (default 3 d);
- an empty or absent `cursor` marks the last page;
- the 201 create response is a top-level object carrying `order_id` (facts §2.1 lists
  its fields; nesting is not stated): anything else raises, so the outcome is recorded
  unknown and resolved by reconciliation;
- the account (primary subaccount) is dedicated to this ledger: any foreign order or
  fill fails closed (facts §2.4: listings without `subaccount` cover all subaccounts);
- a ticker's series is the text before its first `-` ([INF]).
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from ...storage import utc_now
from ..gateway import CancelResult, SubmitResult
from ..ledger import EnvironmentMismatch
from ..money import ZERO, in_exact, positive, utc
from ..portfolio import Holding
from .auth import ClockSkewError, SigningError
from .client import (
    CREATE_ORDER_PATH,
    ORDER_ID,
    DuplicateClientOrderId,
    KalshiError,
    NotFound,
    RequestRejected,
    ResponseFormatError,
)
from .mapping import (
    MappingError,
    PriceRange,
    balance_from_exchange,
    epoch_seconds,
    fill_from_exchange,
    fill_summary,
    from_ms,
    mismatches,
    order_request,
    order_status,
    position_from_exchange,
    venue_order,
)

LIVE_PREFIX = "vi-live-"
PAGE_LIMIT = 200
# Sell fills this long before a positions read still count against it (clock margin).
POSITION_FILL_MARGIN_SECONDS = 60
MAX_PAGES = 100
_CURSOR = re.compile(r"ts:(\d{1,12})\Z")
_SERIES = re.compile(r"[A-Z0-9][A-Z0-9_]{0,59}\Z")

log = logging.getLogger(__name__)


class LookupInconclusive(KalshiError):
    """The venue's answer is not definitive yet; `lookup` must not return None."""


class PaginationIncomplete(KalshiError):
    """A listing could not be read to its last page."""


class ReconciliationMismatch(KalshiError):
    """The venue contradicts the ledger (duplicate exposure, stray order, content)."""


class ForeignActivity(ReconciliationMismatch):
    """A venue order or fill that does not belong to an order of this ledger."""


@dataclass(frozen=True)
class MarketRule:
    """Allowed markets of one series: its price grid and (optional) exchange shard.

    `tickers` empty allows every market of the series."""

    series: str
    price_ranges: tuple
    exchange_index: int | None = None
    tickers: tuple = ()

    def __post_init__(self):
        if not isinstance(self.series, str) or not _SERIES.fullmatch(self.series):
            raise ValueError("series must be an upper-case series ticker")
        ranges = tuple(self.price_ranges)
        if not ranges or not all(isinstance(r, PriceRange) for r in ranges):
            raise ValueError("price_ranges must be a non-empty tuple of PriceRange")
        index = self.exchange_index
        if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
            raise ValueError("exchange_index must be an integer")
        if index is not None and index < 0:
            raise ValueError("exchange_index must be nonnegative")
        tickers = tuple(self.tickers)
        if not all(isinstance(t, str) and t.startswith(self.series + "-") for t in tickers):
            raise ValueError("allowed tickers must belong to their series")
        object.__setattr__(self, "price_ranges", ranges)
        object.__setattr__(self, "tickers", tickers)

    def allows(self, ticker):
        return ticker.startswith(self.series + "-") and (not self.tickers or ticker in self.tickers)


def rule_for(markets, ticker):
    for rule in markets:
        if rule.allows(ticker):
            return rule
    return None


def prepare(order, markets):
    """(OrderRequest, None) or (None, refusal code). Shared with the dry run."""
    rule = rule_for(markets, order.ticker)
    if rule is None:
        return None, "ticker_not_allowed"
    try:
        return order_request(order, rule.price_ranges, rule.exchange_index), None
    except MappingError as exc:
        return None, exc.code


def _seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class KalshiGateway:
    environment = "live"

    def __init__(
        self,
        client,
        *,
        ledger,
        markets,
        max_order_notional,
        max_daily_notional,
        fills_epoch,
        now=utc_now,
        settle_seconds=30,
        max_lookup_age_seconds=3 * 86400,
        lookup_margin_seconds=300,
        fills_overlap_seconds=3600,
        risk_limits_hash=None,
    ):
        if ledger.environment != "live":
            raise EnvironmentMismatch("the Kalshi gateway writes only to a live ledger")
        markets = tuple(markets)
        if not markets or not all(isinstance(m, MarketRule) for m in markets):
            raise ValueError("at least one MarketRule is required")
        self.risk_limits_hash = risk_limits_hash  # the limits acknowledged at activation
        self._positions_at = None  # local time of the last positions() read
        self.max_order_notional = positive(max_order_notional, "max_order_notional")
        self.max_daily_notional = positive(max_daily_notional, "max_daily_notional")
        if self.max_order_notional > self.max_daily_notional:
            raise ValueError("the per-order cap cannot exceed the daily cap")
        self.client = client
        self.ledger = ledger
        self.markets = markets
        self.settle_seconds = _seconds(settle_seconds, "settle_seconds")
        self.max_lookup_age_seconds = _seconds(max_lookup_age_seconds, "max_lookup_age")
        self.lookup_margin_seconds = _seconds(lookup_margin_seconds, "lookup_margin")
        self.fills_overlap_seconds = _seconds(fills_overlap_seconds, "fills_overlap")
        if self.max_lookup_age_seconds <= self.settle_seconds:
            raise ValueError("max_lookup_age_seconds must exceed settle_seconds")
        self._now = now
        self._epoch = utc(fills_epoch, "fills_epoch")
        self._order_ids = {}  # client_order_id -> exchange order id (seen this session)
        self._client_ids = {}  # exchange order id -> client_order_id
        self._accepted = set()  # client ids with a 201 seen: never "not found"
        self._halted = None
        self._positions_seen = False
        self._balance_seen = False
        self._venue_positions = {}  # ticker -> Holding at the last positions() read
        self.shard_balances = {}

    def __repr__(self):
        return f"KalshiGateway(environment={self.client.environment!r}, halted={self.halted})"

    def close(self):
        self.client.close()

    # -- state ---------------------------------------------------------------------

    @property
    def halted(self):
        return self._halted is not None

    def halt(self, reason):
        """Stop new submissions for the life of this instance (kill switch/shutdown)."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("a halt reason is required")
        if self._halted is None:
            log.warning("gateway halted: %s", reason)
            self._halted = reason

    @property
    def startup_reconciled(self):
        """Positions (with resting orders) and balance were read on this instance."""
        return self._positions_seen and self._balance_seen

    def _remember(self, client_order_id, order_id):
        known = self._order_ids.get(client_order_id)
        owner = self._client_ids.get(order_id)
        if (known is not None and known != order_id) or (
            owner is not None and owner != client_order_id
        ):
            raise ReconciliationMismatch(
                f"duplicate exposure: client order {client_order_id} maps to exchange orders "
                f"{known} and {order_id}"
            )
        self._order_ids[client_order_id] = order_id
        self._client_ids[order_id] = client_order_id

    # -- submit --------------------------------------------------------------------

    @in_exact
    def _daily_notional(self, order):
        """Gross notional (quantity x limit) of this UTC day's ledger buy orders,
        including `order`; only orders definitively never placed (refused locally or
        rejected by the exchange) are left out. Covered sells reduce exposure and are not
        capped, so a position can always be exited."""
        day = order.created_at.date()
        total = ZERO
        for state in self.ledger.projection().orders.values():
            placed = not (
                state.status == "rejected"
                and (state.reason or "").startswith(("local:", "exchange:"))
            )
            buy = state.order.action == "buy"
            if placed and buy and state.order.created_at.date() == day:
                total += state.order.quantity * state.order.limit_price
        return total

    def _check_submission(self, order):
        coid = order.client_order_id
        if self._halted is not None:
            return None, "halted"
        if not coid.startswith(LIVE_PREFIX):
            return None, "not_a_live_order"
        if self.ledger.order(coid) != order:
            return None, "order_not_in_ledger"
        if order.action == "buy" and self.ledger.kill_switch().engaged:
            return None, "kill_switch_engaged"
        if not self.startup_reconciled:
            return None, "startup_reconciliation_pending"
        request, refusal = prepare(order, self.markets)
        if refusal is not None:
            return None, refusal
        if order.action == "buy":
            if self.risk_limits_hash is not None:
                _, current = self.ledger.current_limits()
                if current != self.risk_limits_hash:
                    return None, "risk_limits_changed_since_activation"
            if order.quantity * order.limit_price > self.max_order_notional:
                return None, "order_notional_cap"
            if self._daily_notional(order) > self.max_daily_notional:
                return None, "daily_notional_cap"
        if order.action == "sell" and not self._venue_covers(order):
            return None, "venue_position_insufficient"
        return request, None

    @in_exact
    def _venue_covers(self, order):
        """A sell must be covered by the venue's holding of that side (last read), net of
        other active sells: an uncovered `ask`/`bid` would open the opposite side, and
        only immediate-or-cancel orders can carry `reduce_only` (facts §2.1)."""
        held = self._venue_positions.get(order.ticker)
        venue = held.quantity(order.side) if held is not None else ZERO
        if self._positions_at is not None:  # sells filled since that read (conservative)
            since = self._positions_at - timedelta(seconds=POSITION_FILL_MARGIN_SECONDS)
            venue -= sum(
                (
                    f.quantity
                    for f in self.ledger.fills()
                    if f.action == "sell"
                    and (f.ticker, f.side) == (order.ticker, order.side)
                    and f.exchange_ts >= since
                ),
                ZERO,
            )
        committed = sum(
            (
                s.remaining
                for s in self.ledger.projection().active_orders
                if s.order.action == "sell"
                and (s.order.ticker, s.order.side) == (order.ticker, order.side)
                and s.order.client_order_id != order.client_order_id
            ),
            ZERO,
        )
        return order.quantity + committed <= venue

    def submit(self, order):
        request, refusal = self._check_submission(order)
        if refusal is not None:
            log.warning("submission of %s refused locally: %s", order.client_order_id, refusal)
            return SubmitResult("rejected", reason="local:" + refusal)
        try:
            data = self.client.request("POST", CREATE_ORDER_PATH, body=request.body())
        except SigningError as exc:  # raised while building: nothing was sent
            code = "clock_skew" if isinstance(exc, ClockSkewError) else "signing_failed"
            return SubmitResult("rejected", reason="local:" + code)
        except RequestRejected as exc:
            return SubmitResult("rejected", reason=f"exchange:{exc.code}")
        except DuplicateClientOrderId:
            return self._resolve_duplicate(order)
        return self._accept(order, data)

    def _accept(self, order, data):
        order_id = data.get("order_id")
        if not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id):
            raise ResponseFormatError("create response lacks a valid order_id")
        echoed = data.get("client_order_id")
        if echoed is not None and echoed != order.client_order_id:
            raise ReconciliationMismatch("create response carries another client_order_id")
        self._accepted.add(order.client_order_id)
        self._remember(order.client_order_id, order_id)
        try:
            ts = from_ms(data["ts_ms"]) if data.get("ts_ms") is not None else None
        except MappingError:
            ts = None
        return SubmitResult("accepted", exchange_order_id=order_id, exchange_ts=ts)

    def _resolve_duplicate(self, order):
        log.warning("create of %s returned 409; resolving by lookup", order.client_order_id)
        status = self.lookup(order.client_order_id)  # raises unless definitive
        if status is None:
            raise LookupInconclusive("409 Conflict, yet no order with this client id is listed")
        return SubmitResult(
            "accepted",
            exchange_order_id=status.exchange_order_id,
            reason="duplicate_client_order_id_resolved_by_lookup",
        )

    # -- reads ---------------------------------------------------------------------

    def _pages(self, path, params, key):
        items, cursor, seen = [], None, set()
        for _ in range(MAX_PAGES):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            data = self.client.request("GET", path, params=query)
            page = data.get(key)
            if page is None:
                page = []
            if not isinstance(page, list):
                raise ResponseFormatError(f"{path}: {key!r} is not a list")
            items.extend(page)
            cursor = data.get("cursor") or None
            if cursor is None:
                return items
            if not isinstance(cursor, str) or cursor in seen:
                raise PaginationIncomplete(f"{path}: invalid or repeated cursor")
            seen.add(cursor)
        raise PaginationIncomplete(f"{path}: more than {MAX_PAGES} pages")

    def _get_order(self, order_id):
        if not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id):
            raise ResponseFormatError("exchange order id is invalid")
        data = self.client.request("GET", f"/portfolio/orders/{order_id}")
        order = data.get("order")
        if not isinstance(order, dict):
            raise ResponseFormatError("get order response lacks 'order'")
        venue = venue_order(order)
        if venue.order_id != order_id:
            raise ReconciliationMismatch("get order returned another order id")
        return venue

    def _status(self, order, venue):
        problems = mismatches(order, venue)
        if problems:
            raise ReconciliationMismatch(
                f"venue order {venue.order_id} differs from {order.client_order_id}: "
                + ", ".join(problems)
            )
        self._remember(order.client_order_id, venue.order_id)
        return order_status(venue)

    def lookup(self, client_order_id):
        order = self.ledger.order(client_order_id)
        if order is None:
            raise LookupInconclusive("the order is not in this ledger")
        known = self._order_ids.get(client_order_id)
        if known is not None:
            try:
                return self._status(order, self._get_order(known))
            except NotFound:
                pass  # REST lag right after creation; fall back to the listing
        since = order.created_at - timedelta(seconds=self.lookup_margin_seconds)
        items = self._pages(
            "/portfolio/orders",
            {"ticker": order.ticker, "min_ts": epoch_seconds(since), "limit": PAGE_LIMIT},
            "orders",
        )
        matches = [
            venue_order(item)
            for item in items
            if isinstance(item, dict) and item.get("client_order_id") == client_order_id
        ]
        if len({m.order_id for m in matches}) > 1:
            raise ReconciliationMismatch(
                f"duplicate exposure: {len(matches)} venue orders carry {client_order_id}"
            )
        if matches:
            return self._status(order, matches[0])
        if client_order_id in self._accepted or known is not None:
            raise LookupInconclusive("an order the exchange accepted is not listed yet")
        age = self._now() - order.created_at
        if age < timedelta(seconds=self.settle_seconds):
            raise LookupInconclusive("not listed yet; REST reads may lag (settle window)")
        if age > timedelta(seconds=self.max_lookup_age_seconds):
            raise LookupInconclusive("older than the live listing window; reconcile manually")
        return None

    def resting_orders(self):
        """Every resting order of the account (ours or not), as `VenueOrder`s."""
        items = self._pages(
            "/portfolio/orders", {"status": "resting", "limit": PAGE_LIMIT}, "orders"
        )
        return [venue_order(item) for item in items]

    def open_orders(self):
        return [order_status(v) for v in self.resting_orders()]

    # -- cancel --------------------------------------------------------------------

    def cancel(self, client_order_id):
        order = self.ledger.order(client_order_id)
        if order is None:
            return CancelResult("rejected", reason="local:order_not_in_ledger")
        order_id = self._order_ids.get(client_order_id)
        if order_id is None:
            status = self.lookup(client_order_id)  # raises unless definitive
            if status is None:
                return CancelResult("rejected", reason="not_found")
            order_id = status.exchange_order_id
        return self._cancel(order_id, order.ticker, client_order_id)

    def cancel_venue_order(self, venue):
        """Cancel a resting venue order directly (shutdown of orders the ledger lacks)."""
        return self._cancel(venue.order_id, venue.ticker, venue.client_order_id)

    @in_exact
    def _fills_total(self, order_id):
        items = self._pages(
            "/portfolio/fills", {"order_id": order_id, "limit": PAGE_LIMIT}, "fills"
        )
        counts = {}
        for item in items:
            fill_id, owner, count = fill_summary(item)
            if owner != order_id or counts.get(fill_id, count) != count:
                raise ReconciliationMismatch(f"fill listing for {order_id} is inconsistent")
            counts[fill_id] = count
        return sum(counts.values(), ZERO)

    def _cancel(self, order_id, ticker, client_order_id):
        try:
            data = self.client.request(
                "DELETE", f"/portfolio/events/orders/{order_id}", params={"market_ticker": ticker}
            )
        except NotFound:
            data = None  # not cancelable: already terminal or unknown; inspect below
        except SigningError as exc:  # raised while building: nothing was sent
            code = "clock_skew" if isinstance(exc, ClockSkewError) else "signing_failed"
            return CancelResult("rejected", reason="local:" + code)
        except RequestRejected as exc:
            return CancelResult("rejected", reason=f"exchange:{exc.code}")
        try:
            venue = self._get_order(order_id)
        except NotFound:
            return CancelResult("rejected", reason="not_found")
        if venue.client_order_id != client_order_id:
            raise ReconciliationMismatch(f"exchange order {order_id} carries another client id")
        if venue.status == "resting":
            return CancelResult("pending", reason="still_resting")
        if venue.status == "executed":
            return CancelResult("rejected", reason="already_executed")
        delivered = self._fills_total(order_id)
        if delivered > venue.fill_count:
            raise ReconciliationMismatch(f"fills of {order_id} exceed its fill count")
        if delivered < venue.fill_count:
            return CancelResult("pending", reason="fills_not_yet_retrievable")
        state = self.ledger.projection().orders.get(client_order_id)
        if state is not None and state.active and state.filled_quantity != venue.fill_count:
            # The executor releases the reservation on `canceled`: report it only once the
            # ledger holds every fill, so exposure and cash are never understated. The
            # next reconciliation records the terminal state and the fills together.
            return CancelResult("pending", reason="fills_not_yet_recorded")
        ts = None
        if data and data.get("ts_ms") is not None:
            try:
                ts = from_ms(data["ts_ms"])
            except MappingError:
                ts = None
        reason = "canceled" if data is not None else "already_canceled"
        return CancelResult("canceled", reason=reason, exchange_ts=ts)

    # -- fills, positions, balance --------------------------------------------------

    def _cursor_seconds(self, cursor):
        if cursor is None:
            return epoch_seconds(self._epoch)
        match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
        if match is None:
            raise KalshiError("unrecognized fills cursor")
        return int(match.group(1))

    def _fill(self, item):
        _, order_id, _ = fill_summary(item)
        coid = self._client_ids.get(order_id)
        if coid is None:
            try:
                venue = self._get_order(order_id)
            except NotFound:
                raise ForeignActivity(f"fill of unknown exchange order {order_id}") from None
            coid = venue.client_order_id
            order = self.ledger.order(coid) if coid else None
            if order is None or not coid.startswith(LIVE_PREFIX):
                raise ForeignActivity(
                    f"fill of exchange order {order_id}, which is not an order of this ledger"
                )
            self._status(order, venue)
        return fill_from_exchange(item, self.ledger.order(coid))

    def fills_since(self, cursor):
        start = self._cursor_seconds(cursor)
        min_ts = max(epoch_seconds(self._epoch), start - self.fills_overlap_seconds)
        items = self._pages("/portfolio/fills", {"min_ts": min_ts, "limit": PAGE_LIMIT}, "fills")
        fills = {}
        for item in items:
            fill = self._fill(item)
            previous = fills.get(fill.fill_id)
            if previous is not None and previous != fill:
                raise ReconciliationMismatch(f"conflicting duplicates of fill {fill.fill_id}")
            fills[fill.fill_id] = fill
        ordered = sorted(fills.values(), key=lambda f: (f.exchange_ts, f.fill_id))
        newest = max((epoch_seconds(f.exchange_ts) for f in ordered), default=start)
        watermark = max(start, min(newest, epoch_seconds(self._now())))
        return ordered, f"ts:{watermark}"

    def positions(self):
        items = self._pages("/portfolio/positions", {"limit": PAGE_LIMIT}, "market_positions")
        holdings, seen = {}, set()
        for item in items:
            ticker, position = position_from_exchange(item)
            if ticker in seen:
                raise ReconciliationMismatch(f"duplicate position rows for {ticker}")
            seen.add(ticker)
            if position:
                holdings[ticker] = Holding(
                    ticker, yes_qty=max(position, ZERO), no_qty=max(-position, ZERO)
                )
        read_at = self._now()
        resting = self.resting_orders()
        active = {c for c, s in self.ledger.projection().orders.items() if s.active}
        strays = sorted(v.order_id for v in resting if v.client_order_id not in active)
        if strays:  # startup reconciliation is not complete while strays exist
            log.warning("resting venue orders not active in the ledger: %s", ", ".join(strays))
            raise ReconciliationMismatch(
                "resting venue orders not active in the ledger: " + ", ".join(strays)
            )
        self._venue_positions = holdings
        self._positions_at = read_at
        self._positions_seen = True
        return holdings

    def balance(self):
        total, shards = balance_from_exchange(self.client.request("GET", "/portfolio/balance"))
        self.shard_balances = shards
        self._balance_seen = True
        return total

    def exchange_status(self):
        """Public exchange status: overall and per-shard trading flags (facts §9)."""
        data = self.client.request("GET", "/exchange/status")
        flags = {}
        for name in ("exchange_active", "trading_active"):
            if not isinstance(data.get(name), bool):
                raise ResponseFormatError(f"exchange status lacks {name!r}")
            flags[name] = data[name]
        shards = {}
        for item in data.get("exchange_index_statuses") or ():
            index, active = item.get("exchange_index"), item.get("trading_active")
            if isinstance(index, int) and not isinstance(index, bool) and isinstance(active, bool):
                shards[index] = active
        return {**flags, "shards": shards}
