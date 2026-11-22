"""Event-driven paper simulator: `PaperExchange` (an ExchangeGateway) and `run_paper`.

One merged stream of snapshots, forecasts, settlements and the simulator's own order
arrivals is processed in `(ts, kind rank, key)` order. Each input is processed in one
ledger transaction together with its `processed_inputs` key, so a restarted run skips
what is done and reproduces the same projections. Randomness (latency) is drawn from
an RNG keyed by `(seed, client order id)`, so it does not depend on processing history.

Execution model (all conservative by default):
- decisions see only snapshots with `as_of <= decision time`; an order arrives at
  `decided_at + decision latency + submit latency`;
- book at arrival: `worst_of_bracketing` (cumulative-depth minimum of the latest
  snapshot <= arrival and the first >= arrival) or `prior_snapshot`; a gap above
  `max_snapshot_gap_seconds` makes the order `execution_unidentifiable` (no fill);
- taker fills walk the opposite ladder up to the limit, consuming `depth_haircut` of
  each level plus `extra_slippage_ticks`; simulated fills earlier in the same snapshot
  interval are assumed to have consumed the best levels first; IOC remainders cancel,
  GTC remainders rest;
- resting orders fill only under `maker_fill_model`: `none` (default), `trade_through`
  (a later snapshot shows the counterparty strictly through our limit) or
  `optimistic_touch` (labelled upper bound);
- settlements pay out per the portfolio rules and expire the market's open orders.
"""

import hashlib
import heapq
import random
import tempfile
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import timedelta
from itertools import product
from pathlib import Path

from ..provenance import source_hash
from .executor import Executor
from .fees import DEFAULT_FEE_SCHEDULE, PROVISIONAL_SCHEDULES, FeeSchedule
from .gateway import CancelResult, GatewayEvent, OrderStatus, SubmitResult
from .instruments import Market, identifier
from .ledger import Ledger
from .marketdata import BookSnapshot, assume_depth
from .money import (
    ONE,
    ZERO,
    ceil_to,
    dec,
    digest,
    floor_to,
    in_exact,
    nonnegative,
    opt_dec,
    plain,
    positive,
    price,
    seconds,
    text,
    utc,
)
from .orders import TERMINAL, Fill, OrderIntent, opposite
from .portfolio import CashEvent, Settlement
from .report import NOTICE, SensitivityReport, paper_report
from .risk import (
    SIMULATION_ASSUMPTIONS,
    ForecastStatus,
    RiskEngine,
    RiskLimits,
    simulation_defaults,
)

BOOK_RULES = ("prior_snapshot", "worst_of_bracketing")
MAKER_MODELS = ("none", "trade_through", "optimistic_touch")
PRICING = ("take", "join")
MAX_SCENARIOS = 256
_RANK = {"snapshot": 0, "arrival": 1, "forecast": 2, "settlement": 3}
_INPUT_PREFIXES = ("snapshot:", "forecast:", "arrive:", "settlement:")


def _ms_range(value, name):
    lo, hi = value
    for v in (lo, hi):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 3_600_000:
            raise ValueError(f"{name}: milliseconds must be integers within [0, 3600000]")
    if lo > hi:
        raise ValueError(f"{name}: lower bound exceeds upper bound")
    return (lo, hi)


@dataclass(frozen=True)
class LatencyModel:
    """Fixed (lo == hi) or seeded-uniform integer-millisecond latencies."""

    decision_ms: tuple = (0, 0)
    submit_ms: tuple = (250, 250)

    def __post_init__(self):
        object.__setattr__(self, "decision_ms", _ms_range(tuple(self.decision_ms), "decision"))
        object.__setattr__(self, "submit_ms", _ms_range(tuple(self.submit_ms), "submit"))

    def draw(self, seed, key):
        """Total latency for one order; depends only on (seed, key)."""
        material = hashlib.sha256(f"{seed}:{key}".encode()).digest()
        rng = random.Random(int.from_bytes(material[:8], "big"))
        total = rng.randint(*self.decision_ms) + rng.randint(*self.submit_ms)
        return timedelta(milliseconds=total)


@dataclass(frozen=True)
class StrategyConfig:
    """Declarative edge-threshold strategy: buy the side with the larger edge
    (probability - price - per-contract fee) when it reaches `min_edge`.
    `take` crosses at the ask (IOC); `join` rests at the bid (GTC, post-only)."""

    strategy_id: str = "edge-threshold-v1"
    min_edge: object = dec("0.02")
    sides: tuple = ("yes", "no")
    pricing: str = "take"
    max_quantity: object = None

    def __post_init__(self):
        identifier(self.strategy_id, "strategy_id")
        object.__setattr__(self, "min_edge", dec(self.min_edge, "min_edge"))
        sides = tuple(self.sides)
        if not sides or not set(sides) <= {"yes", "no"}:
            raise ValueError("sides must be a non-empty subset of ('yes', 'no')")
        object.__setattr__(self, "sides", sides)
        if self.pricing not in PRICING:
            raise ValueError(f"pricing must be one of {PRICING}")
        quantity = opt_dec(self.max_quantity, "max_quantity")
        if quantity is not None and quantity <= 0:
            raise ValueError("max_quantity must be positive")
        object.__setattr__(self, "max_quantity", quantity)


@dataclass(frozen=True)
class ForecastInput:
    forecast_id: str
    ticker: str
    probability: object
    computed_at: object
    model_id: str
    artifact_hash: str = ""
    standard_error: object = None

    def __post_init__(self):
        identifier(self.forecast_id, "forecast_id")
        identifier(self.ticker, "ticker")
        object.__setattr__(self, "probability", price(self.probability, "probability"))
        object.__setattr__(self, "computed_at", utc(self.computed_at, "computed_at"))
        object.__setattr__(self, "standard_error", opt_dec(self.standard_error, "standard_error"))


@dataclass(frozen=True)
class PaperInputs:
    """Everything a run reads. `label` says where the inputs came from (e.g.
    'synthetic:...' or 'archive:<identity>')."""

    label: str
    markets: tuple
    snapshots: tuple
    forecasts: tuple
    settlements: tuple = ()

    def digests(self):
        return {
            "markets": digest(sorted((plain(m) for m in self.markets), key=lambda m: m["ticker"])),
            "snapshots": digest([plain(s) for s in self.snapshots]),
            "forecasts": digest([plain(f) for f in self.forecasts]),
            "settlements": digest([plain(s) for s in self.settlements]),
        }

    def digest(self):
        return digest({"label": self.label, **self.digests()})


@dataclass(frozen=True)
class PaperConfig:
    initial_cash: object = dec(1000)
    seed: int = 0
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    limits: RiskLimits | None = None  # None: simulation_defaults(initial_cash)
    fee_schedule: FeeSchedule = DEFAULT_FEE_SCHEDULE
    latency: LatencyModel = field(default_factory=LatencyModel)
    book_state_rule: str = "worst_of_bracketing"
    max_snapshot_gap_seconds: object = dec(120)
    depth_haircut: object = dec("0.5")
    extra_slippage_ticks: int = 0
    maker_fill_model: str = "none"
    # SCENARIO for depth-unknown snapshots (candles/quotes without sizes): contracts assumed
    # at the best level only. None (default): such snapshots never supply depth.
    assumed_depth: object = None
    clip_to_limits: bool = False
    probability_haircut: object = ZERO
    z: object = ZERO

    def __post_init__(self):
        object.__setattr__(self, "initial_cash", positive(self.initial_cash, "initial_cash"))
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("seed must be an integer")
        if self.book_state_rule not in BOOK_RULES:
            raise ValueError(f"book_state_rule must be one of {BOOK_RULES}")
        if self.maker_fill_model not in MAKER_MODELS:
            raise ValueError(f"maker_fill_model must be one of {MAKER_MODELS}")
        haircut = dec(self.depth_haircut, "depth_haircut")
        if not ZERO < haircut <= ONE:
            raise ValueError("depth_haircut must be within (0, 1]")
        object.__setattr__(self, "depth_haircut", haircut)
        object.__setattr__(
            self,
            "max_snapshot_gap_seconds",
            positive(self.max_snapshot_gap_seconds, "max_snapshot_gap_seconds"),
        )
        ticks = self.extra_slippage_ticks
        if isinstance(ticks, bool) or not isinstance(ticks, int) or not 0 <= ticks <= 100:
            raise ValueError("extra_slippage_ticks must be an integer within [0, 100]")
        object.__setattr__(
            self,
            "probability_haircut",
            nonnegative(self.probability_haircut, "probability_haircut"),
        )
        object.__setattr__(self, "z", nonnegative(self.z, "z"))
        if self.assumed_depth is not None:
            object.__setattr__(self, "assumed_depth", positive(self.assumed_depth, "assumed_depth"))

    def effective_limits(self):
        return self.limits if self.limits is not None else simulation_defaults(self.initial_cash)

    def to_dict(self):
        data = plain(self)
        data["limits"] = plain(self.effective_limits())
        data["limits_source"] = "configured" if self.limits is not None else "simulation_defaults"
        return data

    def digest(self):
        return digest(self.to_dict())


def assumptions(config):
    """The run's modelling assumptions, as stated in every manifest."""
    lat = config.latency
    maker = {
        "none": "Maker model none: resting orders never fill.",
        "trade_through": "Maker model trade_through: a resting order fills at its limit only "
        "when a later snapshot shows the counterparty strictly through the limit, capped by "
        "the haircut depth of that best level.",
        "optimistic_touch": "Maker model optimistic_touch is an UPPER BOUND: resting orders "
        "fill when the counterparty merely touches the limit; touching is not evidence of a "
        "maker fill.",
    }[config.maker_fill_model]
    out = [
        NOTICE,
        "Paper network is lossless: a durable submit_requested reaches the simulated venue "
        "after the modelled latency.",
        f"Latency per order: decision {lat.decision_ms[0]}-{lat.decision_ms[1]} ms plus "
        f"submit {lat.submit_ms[0]}-{lat.submit_ms[1]} ms, seeded by (seed, client order id).",
        f"Book state at arrival: {config.book_state_rule}; a snapshot gap above "
        f"{text(config.max_snapshot_gap_seconds)} s makes execution unidentifiable (no fill).",
        "Decisions use only snapshots with as_of <= decision time; execution may use the "
        "first snapshot after arrival, as a conservative execution model only.",
        f"Taker fills consume {text(config.depth_haircut)} of displayed depth per level with "
        f"{config.extra_slippage_ticks} extra slippage tick(s); earlier simulated fills in the "
        "same snapshot interval are assumed to have taken the best levels.",
        maker,
        "Simulated orders do not move the archived book beyond the depth haircut and the "
        "consumed-liquidity accounting (no market-impact model).",
        f"Fee schedule {config.fee_schedule.version} (verified={config.fee_schedule.verified}).",
        *config.fee_schedule.assumptions,
        "Markets without settlement evidence at run end are reported open at conservative "
        "best-bid marks.",
        "Depth-unknown snapshots (candles or quotes without sizes) supply no depth."
        if config.assumed_depth is None
        else f"SCENARIO: depth-unknown snapshots are assumed to show "
        f"{text(config.assumed_depth)} contracts at the best level only; this is an "
        "assumption, not evidence of executable size.",
    ]
    if config.limits is None:
        out.extend(SIMULATION_ASSUMPTIONS)
    return out


def propose(strategy, forecast, market, snapshot, fee_schedule):
    """(intent, None) or (None, reason). Pure: uses only the given snapshot."""
    if snapshot is None:
        return None, "no_quote"
    candidates = []
    for side in strategy.sides:
        q = forecast.probability if side == "yes" else ONE - forecast.probability
        if strategy.pricing == "take":
            raw, liquidity = snapshot.ask(side), "taker"
            limit = None if raw is None else ceil_to(raw, market.tick)
        else:
            raw, liquidity = snapshot.bid(side), "maker"
            limit = None if raw is None else floor_to(raw, market.tick)
        if limit is None or not market.min_price <= limit <= market.max_price:
            continue
        edge = q - limit - fee_schedule.fee_per_contract(limit, liquidity, market.qty_step)
        candidates.append((edge, side == "yes", side, limit))
    if not candidates:
        return None, "no_price"
    edge, _, side, limit = max(candidates)
    if edge < strategy.min_edge:
        return None, "edge_below_threshold"
    take = strategy.pricing == "take"
    intent = OrderIntent(
        strategy_id=strategy.strategy_id,
        forecast_ref={
            "forecast_id": forecast.forecast_id,
            "model_id": forecast.model_id,
            "artifact_hash": forecast.artifact_hash,
            "probability": text(forecast.probability),
            "computed_at": forecast.computed_at.isoformat(),
        },
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        side=side,
        action="buy",
        limit_price=limit,
        max_quantity=strategy.max_quantity,
        time_in_force="ioc" if take else "gtc",
        post_only=not take,
        created_at=forecast.computed_at,
        rationale=f"edge {text(edge)} >= threshold {text(strategy.min_edge)}",
    )
    return intent, None


def _merge_worst(a, b):
    """Bid ladder whose cumulative depth at every price is the minimum of a and b."""
    prices = sorted({p for p, _ in a} | {p for p, _ in b}, reverse=True)
    qa, qb = dict(a), dict(b)
    out, cum_a, cum_b, prev = [], ZERO, ZERO, ZERO
    for p in prices:
        cum_a += qa.get(p, ZERO)
        cum_b += qb.get(p, ZERO)
        worst = min(cum_a, cum_b)
        if worst > prev:
            out.append((p, worst - prev))
            prev = worst
    return out


@in_exact
def worst_of(a, b):
    """Conservative combination of two snapshots of one market."""
    if not (a.depth_known and b.depth_known):
        return BookSnapshot(
            a.ticker, max(a.as_of, b.as_of), (), (), "synthetic", f"worst({a.ref},{b.ref})", False
        )
    return BookSnapshot(
        a.ticker,
        max(a.as_of, b.as_of),
        _merge_worst(a.yes_bids, b.yes_bids),
        _merge_worst(a.no_bids, b.no_bids),
        "synthetic",
        f"worst({a.ref},{b.ref})",
        True,
    )


class PaperExchange:
    """Deterministic simulated venue. Its state is derived from the ledger and the run
    inputs, so a restarted run sees the same venue; it never writes the ledger."""

    environment = "paper"

    def __init__(self, ledger, config, inputs):
        self.ledger, self.config = ledger, config
        self.markets = {m.ticker: m for m in inputs.markets}
        books = defaultdict(list)
        for snap in sorted(inputs.snapshots, key=lambda s: (s.as_of, s.ref)):
            books[snap.ticker].append(snap)
        self.books = dict(books)
        self.times = {t: [s.as_of for s in snaps] for t, snaps in self.books.items()}
        self.now = None
        self.scheduled = []

    def arrival_time(self, order):
        return order.created_at + self.config.latency.draw(self.config.seed, order.client_order_id)

    def restore(self):
        """Arrivals still owed after a restart: submitted and not yet arrived. Orders that
        turned terminal before arriving are included: their arrival is a recorded no-op,
        exactly as in an uninterrupted run."""
        out = []
        for coid, state in sorted(self.ledger.projection().orders.items()):
            if state.submit_requested and not self._arrived(coid):
                out.append((self.arrival_time(state.order), coid))
        return out

    def _arrived(self, coid):
        return self.ledger.processed(f"arrive:{coid}") is not None

    # -- ExchangeGateway --------------------------------------------------------------

    def submit(self, order):
        self.scheduled.append((self.arrival_time(order), order.client_order_id))
        return SubmitResult("submitted", exchange_order_id=f"paper-{order.client_order_id}")

    def cancel(self, client_order_id):
        state = self.ledger.projection().orders.get(client_order_id)
        if state is None or not state.active:
            return CancelResult("rejected", reason="not_active")
        return CancelResult("canceled", reason="canceled_by_request", exchange_ts=self.now)

    def lookup(self, client_order_id):
        state = self.ledger.projection().orders.get(client_order_id)
        if state is None or not state.submit_requested:
            return None
        if state.status in TERMINAL:
            status = state.status
        else:
            status = "open" if self._arrived(client_order_id) else "pending"
        return OrderStatus(
            client_order_id, status, state.filled_quantity, f"paper-{client_order_id}"
        )

    def open_orders(self):
        return [
            self.lookup(coid)
            for coid, s in sorted(self.ledger.projection().orders.items())
            if s.active and self._arrived(coid)
        ]

    def fills_since(self, cursor):
        fills = [f for f in self.ledger.fills() if f.source == "simulated"]
        start = int(cursor or 0)
        return fills[start:], (str(len(fills)) if fills else cursor)

    def positions(self):
        return dict(self.ledger.projection().portfolio.holdings)

    def balance(self):
        return self.ledger.projection().portfolio.cash

    # -- matching ---------------------------------------------------------------------

    def _event(self, order, kind, ts, reason):
        coid = order.client_order_id
        event_id = "sim:" + digest({"order": coid, "kind": kind, "at": ts, "reason": reason})
        return GatewayEvent(event_id, coid, kind, f"paper-{coid}", None, ts, {"reason": reason})

    def _fill(self, order, ts, index, fill_price, quantity, liquidity):
        fill_id = "sim:" + digest(
            {
                "order": order.client_order_id,
                "at": ts,
                "level": index,
                "price": fill_price,
                "qty": quantity,
                "liq": liquidity,
            }
        )
        fee = self.config.fee_schedule.fee(fill_price, quantity, liquidity)
        return Fill(
            fill_id,
            order.client_order_id,
            order.ticker,
            order.side,
            order.action,
            fill_price,
            quantity,
            fee,
            liquidity,
            ts,
            "simulated",
        )

    def book_at(self, ticker, ts):
        """(book, None, sources) or (None, reason, ()) per the book rule; `sources` are
        the raw snapshot(s) the book was built from."""
        snaps, times = self.books.get(ticker, []), self.times.get(ticker, [])
        i, j = bisect_right(times, ts) - 1, bisect_left(times, ts)
        if self.config.assumed_depth is not None:
            snaps = [assume_depth(s, self.config.assumed_depth) for s in snaps]
        prior = snaps[i] if i >= 0 else None
        gap = self.config.max_snapshot_gap_seconds
        if self.config.book_state_rule == "prior_snapshot":
            if prior is None or seconds(ts - prior.as_of) > gap:
                return None, "execution_unidentifiable", ()
            return prior, None, (prior,)
        after = snaps[j] if j < len(snaps) else None
        if prior is None or after is None or seconds(after.as_of - prior.as_of) > gap:
            return None, "execution_unidentifiable", ()
        if prior is after:
            return prior, None, (prior,)
        return worst_of(prior, after), None, (prior, after)

    @staticmethod
    def _ladder(order):
        return opposite(order.side) if order.action == "buy" else order.side

    def _consumed(self, order, since, ts):
        ladder = self._ladder(order)
        total = ZERO
        for f in self.ledger.fills():
            if f.ticker != order.ticker or f.liquidity != "taker":
                continue
            if f.client_order_id == order.client_order_id:
                continue
            if (opposite(f.side) if f.action == "buy" else f.side) == ladder:
                if since <= f.exchange_ts <= ts:
                    total += f.quantity
        return total

    @in_exact
    def _executable(self, book, order, market, consumed):
        """(price, quantity) taker levels within the limit, after haircut, slippage and
        consumed liquidity; also whether the raw book crosses the limit."""
        slip = self.config.extra_slippage_ticks * market.tick
        raw = book.bids(self._ladder(order))
        out, crosses = [], False
        for level_price, displayed in raw:
            if order.action == "buy":
                touch = ONE - level_price
                fill_price = ceil_to(touch + slip, market.tick)
                if touch <= order.limit_price:
                    crosses = True
                if fill_price > order.limit_price:
                    break
            else:
                touch = level_price
                fill_price = floor_to(touch - slip, market.tick)
                if touch >= order.limit_price:
                    crosses = True
                if fill_price < order.limit_price:
                    break
            available = floor_to(displayed * self.config.depth_haircut, market.qty_step)
            used = min(consumed, available)
            consumed -= used
            if available - used > 0:
                out.append((fill_price, available - used))
        return out, crosses

    @in_exact
    def arrive(self, coid, ts):
        """Items produced when an order reaches the matching engine."""
        projection = self.ledger.projection()
        state = projection.orders.get(coid)
        if state is None or not state.active:
            return []
        order = state.order
        market = self.markets[order.ticker]
        if order.ticker in projection.portfolio.settlements or not market.is_open(ts):
            return [self._event(order, "rejected", ts, "market_closed")]
        book, reason, sources = self.book_at(order.ticker, ts)
        if book is None:
            return [self._event(order, "rejected", ts, reason)]
        consumed = self._consumed(order, sources[0].as_of, ts)
        levels, _ = self._executable(book, order, market, consumed)
        # Post-only is refused if the order would cross either source snapshot (the
        # conservative direction for a maker order is to assume it crosses).
        if order.post_only and any(
            self._executable(snap, order, market, ZERO)[1] for snap in sources
        ):
            return [self._event(order, "rejected", ts, "post_only_would_cross")]
        items = [self._event(order, "accepted", ts, "arrived")]
        remaining = state.remaining
        for index, (fill_price, available) in enumerate(levels):
            take = min(available, remaining)
            if take > 0:
                items.append(self._fill(order, ts, index, fill_price, take, "taker"))
                remaining -= take
            if remaining == 0:
                break
        if remaining > 0 and order.time_in_force == "ioc":
            items.append(self._event(order, "canceled", ts, "ioc_remainder"))
        return items

    @in_exact
    def on_snapshot(self, snap):
        """Maker fills for resting orders implied by a later snapshot."""
        model = self.config.maker_fill_model
        market = self.markets[snap.ticker]
        projection = self.ledger.projection()
        if model == "none" or not snap.depth_known or not market.is_open(snap.as_of):
            return []
        if snap.ticker in projection.portfolio.settlements:
            return []
        resting = sorted(
            (
                s
                for s in projection.orders.values()
                if s.status == "open"
                and s.order.ticker == snap.ticker
                and s.accepted_at is not None
                and s.accepted_at < snap.as_of
            ),
            key=lambda s: (s.order.created_at, s.order.client_order_id),
        )
        # Maker liquidity already granted at this instant (e.g. by another snapshot of the
        # same market with the same as_of) is not granted twice.
        used, items = defaultdict(lambda: ZERO), []
        for f in self.ledger.fills():
            if f.ticker == snap.ticker and f.liquidity == "maker" and f.exchange_ts == snap.as_of:
                used[opposite(f.side) if f.action == "buy" else f.side] += f.quantity
        for state in resting:
            order = state.order
            ladder = self._ladder(order)
            levels = snap.bids(ladder)
            if not levels:
                continue
            level_price, displayed = levels[0]
            if order.action == "buy":
                counter = ONE - level_price
                through = counter < order.limit_price
                touch = counter <= order.limit_price
            else:
                counter = level_price
                through = counter > order.limit_price
                touch = counter >= order.limit_price
            if not (through if model == "trade_through" else touch):
                continue
            cap = floor_to(displayed * self.config.depth_haircut, market.qty_step) - used[ladder]
            take = min(state.remaining, cap)
            if take <= 0:
                continue
            used[ladder] += take
            items.append(self._fill(order, snap.as_of, 0, order.limit_price, take, "maker"))
        return items

    def on_settlement(self, settlement):
        """Expire every active order of the settled market."""
        return [
            self._event(s.order, "expired", settlement.effective_at, "market_settled")
            for s in self.ledger.projection().active_orders
            if s.order.ticker == settlement.ticker
        ]


def _stream(inputs):
    items, keys = [], set()
    for snap in inputs.snapshots:
        key = f"snapshot:{snap.ticker}:{snap.as_of.isoformat()}:{snap.ref}"
        items.append((snap.as_of, _RANK["snapshot"], key, "snapshot", snap))
    for forecast in inputs.forecasts:
        key = f"forecast:{forecast.forecast_id}"
        items.append((forecast.computed_at, _RANK["forecast"], key, "forecast", forecast))
    for settlement in inputs.settlements:
        key = f"settlement:{settlement.settlement_id}"
        items.append((settlement.effective_at, _RANK["settlement"], key, "settlement", settlement))
    for item in items:
        if item[2] in keys:
            raise ValueError(f"duplicate input key {item[2]}")
        keys.add(item[2])
    return items


def _validate(inputs):
    if not isinstance(inputs.label, str) or not inputs.label:
        raise ValueError("inputs need a provenance label")
    markets = {}
    for m in inputs.markets:
        if not isinstance(m, Market) or m.ticker in markets:
            raise ValueError("markets must be unique Market records")
        markets[m.ticker] = m
    for record in [*inputs.snapshots, *inputs.forecasts, *inputs.settlements]:
        if record.ticker not in markets:
            raise ValueError(f"input for unknown market {record.ticker}")
    for snap in inputs.snapshots:
        if not isinstance(snap, BookSnapshot):
            raise TypeError("snapshots must be BookSnapshot records")
    for s in inputs.settlements:
        if not isinstance(s, Settlement):
            raise TypeError("settlements must be Settlement records")
    return markets


def run_paper(config, inputs, ledger_path, *, fault=None):
    """Run (or resume) a paper simulation into the ledger at `ledger_path`.

    The ledger records the run identity; resuming with a different configuration or
    different inputs raises `LedgerConflict`. Returns a `PaperRunReport`.
    """
    markets = _validate(inputs)
    stream = _stream(inputs)
    if not stream:
        raise ValueError("a paper run needs at least one input")
    start = min(item[0] for item in stream)
    limits = config.effective_limits()
    ledger = Ledger(ledger_path, "paper")
    ledger.fault = fault
    try:
        with ledger.transaction("setup"):
            ledger.mark_processed(
                "run:identity", {"config": config.digest(), "inputs": inputs.digest()}, start
            )
            ledger.append_cash_event(
                CashEvent(
                    "paper:initial-deposit", "deposit", config.initial_cash, start, "paper bankroll"
                )
            )
            ledger.record_limits(limits, "paper-config", start)
        exchange = PaperExchange(ledger, config, inputs)
        engine = RiskEngine(
            "paper",
            clip_to_limits=config.clip_to_limits,
            probability_haircut=config.probability_haircut,
            z=config.z,
        )
        executor = Executor(ledger, exchange, engine, config.fee_schedule)
        heap = list(stream)
        for arrive, coid in exchange.restore():
            heap.append((arrive, _RANK["arrival"], f"arrive:{coid}", "arrival", coid))
        heapq.heapify(heap)
        done_at = [
            utc(r["at"])
            for k, r in ledger.processed_inputs().items()
            if k.startswith(_INPUT_PREFIXES)
        ]
        resume_at = max(done_at, default=start)
        exchange.now = resume_at
        executor.reconcile(now=resume_at)
        latest, equity_history = {}, []
        while heap:
            ts, _, key, kind, payload = heapq.heappop(heap)
            exchange.now = ts
            if kind == "snapshot":
                latest[payload.ticker] = payload
            done = ledger.processed(key)
            if done is not None:
                if isinstance(done, dict) and "equity" in done:
                    equity_history.append((ts, dec(done["equity"])))
                continue
            mark = _Mark(ledger, latest)
            if kind == "snapshot":
                executor.ingest(
                    exchange.on_snapshot(payload), now=ts, input_key=key, checkpoint=mark
                )
            elif kind == "arrival":
                executor.ingest(
                    exchange.arrive(payload, ts), now=ts, input_key=key, checkpoint=mark
                )
            elif kind == "settlement":
                executor.ingest(
                    exchange.on_settlement(payload),
                    now=ts,
                    settlement=payload,
                    input_key=key,
                    checkpoint=mark,
                )
            else:
                market = markets[payload.ticker]
                intent, reason = propose(
                    config.strategy,
                    payload,
                    market,
                    latest.get(payload.ticker),
                    config.fee_schedule,
                )
                if intent is None:
                    ledger.mark_processed(key, {"intent": None, "reason": reason}, ts)
                else:
                    forecast = ForecastStatus(
                        payload.ticker,
                        payload.probability,
                        payload.computed_at,
                        payload.model_id,
                        True,
                        payload.standard_error,
                    )
                    executor.place(
                        intent,
                        now=ts,
                        market=market,
                        snapshot=latest.get(payload.ticker),
                        forecast=forecast,
                        marks=latest,
                        markets=markets,
                        equity_history=equity_history,
                        input_key=key,
                        checkpoint={"intent": intent.intent_id},
                    )
            if mark.value is not None:
                equity_history.append((ts, dec(mark.value["equity"])))
            for arrive, coid in exchange.scheduled:
                heapq.heappush(heap, (arrive, _RANK["arrival"], f"arrive:{coid}", "arrival", coid))
            exchange.scheduled.clear()
        manifest = {
            "kind": "paper-run",
            "simulated": True,
            "notice": NOTICE,
            "environment": "paper",
            "input_label": inputs.label,
            "input_digests": inputs.digests(),
            "config_digest": config.digest(),
            "config": config.to_dict(),
            "fee_schedule": config.fee_schedule.manifest(),
            "assumptions": assumptions(config),
            "seed": config.seed,
            "code_hash": source_hash(),
        }
        return paper_report(ledger, manifest=manifest, marks=latest)
    finally:
        ledger.close()


class _Mark:
    """Checkpoint computed inside an input's transaction, after its effects."""

    def __init__(self, ledger, marks):
        self.ledger, self.marks, self.value = ledger, marks, None

    def __call__(self):
        port = self.ledger.projection().portfolio
        self.value = {
            "equity": text(port.equity(self.marks)),
            "equity_mid": text(port.equity(self.marks, "mid")),
            "cash": text(port.cash),
            "exposure": text(port.remaining_cost + port.reserved_cash),
        }
        return self.value


def default_grid(config):
    """Fee x slippage x latency x fill-model scenarios (both provisional fee schedules)."""
    slow = LatencyModel(
        decision_ms=config.latency.decision_ms,
        submit_ms=(config.latency.submit_ms[0] + 1000, config.latency.submit_ms[1] + 1000),
    )
    return {
        "fee_schedules": PROVISIONAL_SCHEDULES,
        "extra_slippage_ticks": (0, 1),
        "latency": (config.latency, slow),
        "maker_fill_model": MAKER_MODELS,
    }


def sensitivity(config, inputs, grid=None, *, directory=None):
    """Rerun `config` over the grid into disposable ledgers; tabulate the results.

    Deterministic: the same config, inputs, grid and seed give byte-identical JSON.
    """
    grid = dict(grid or default_grid(config))
    axes = ("fee_schedules", "extra_slippage_ticks", "latency", "maker_fill_model")
    if not set(axes) <= set(grid) <= set(axes) | {"assumed_depth"}:
        raise ValueError(f"grid must define exactly {axes} (and optionally assumed_depth)")
    grid.setdefault("assumed_depth", (config.assumed_depth,))
    axes = axes + ("assumed_depth",)
    scenarios = list(product(*(tuple(grid[a]) for a in axes)))
    if not scenarios or len(scenarios) > MAX_SCENARIOS:
        raise ValueError(f"grid must define 1..{MAX_SCENARIOS} scenarios")
    rows = []
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        for index, (fees, ticks, latency, maker, depth) in enumerate(scenarios):
            scenario = replace(
                config,
                fee_schedule=fees,
                extra_slippage_ticks=ticks,
                latency=latency,
                maker_fill_model=maker,
                assumed_depth=depth,
            )
            report = run_paper(scenario, inputs, Path(tmp) / f"scenario-{index:03d}.sqlite3")
            rows.append(
                {
                    "scenario": {
                        "fee_schedule": fees.version,
                        "fee_verified": fees.verified,
                        "extra_slippage_ticks": ticks,
                        "latency": plain(latency),
                        "maker_fill_model": maker,
                        "assumed_depth": None if depth is None else plain(depth),
                    },
                    "config_digest": scenario.digest(),
                    "pnl": report.pnl,
                    "orders": len(report.orders),
                    "fills": len(report.fills),
                    "rejections_by_reason": report.rejections_by_reason,
                    "unidentifiable": report.unidentifiable,
                    "verification_failures": len(report.verification),
                }
            )
    manifest = {
        "kind": "paper-sensitivity",
        "simulated": True,
        "notice": NOTICE,
        "input_label": inputs.label,
        "input_digests": inputs.digests(),
        "base_config_digest": config.digest(),
        "seed": config.seed,
        "code_hash": source_hash(),
        "grid": {
            a: [plain(v) if not isinstance(v, FeeSchedule) else v.manifest() for v in grid[a]]
            for a in axes
        },
        "assumptions": assumptions(config),
    }
    return SensitivityReport(manifest, rows)
