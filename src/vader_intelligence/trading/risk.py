"""Authoritative pre-trade risk: limits, ordered checks, approvals and rejections.

`RiskEngine.evaluate(intent, ctx)` is pure and runs the checks of spec §6 in a fixed
order, collecting every failing reason. Only it creates `Approval`s; the executor
records each approval or rejection (both are audit records) and only an unused
approval can become an order. Limits come from operator-written `risk_limit_versions`
rows; there is no other way to change them.

Exposure is maximum loss: the cost basis of holdings (fees included) plus the cash
reserved by open orders. Event exposure sums both contracts of an event (the
conservative sum of maximum losses); group exposure sums the dependence cluster.
Paper limits may be partially unset (unset = not enforced, recorded as such); live
requires every limit (`limits_unset` otherwise, so an unconfigured live deployment
rejects every order).
"""

from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from decimal import Decimal

from . import kelly
from .instruments import correlation_groups, same_team_same_date
from .money import (
    ONE,
    ZERO,
    dec,
    digest,
    floor_to,
    frozen_mapping,
    in_exact,
    nonnegative,
    on_grid,
    opt_dec,
    plain,
    positive,
    price,
    seconds,
    steps,
    text,
    utc,
)
from .orders import ENVIRONMENTS, opposite

REASONS = (
    "kill_switch_engaged",
    "limits_unset",
    "state_unknown",
    "market_unknown",
    "market_not_open",
    "data_stale",
    "forecast_stale",
    "forecast_unavailable",
    "price_off_grid",
    "price_out_of_bounds",
    "price_deviation",
    "edge_below_minimum",
    "size_zero",
    "insufficient_contracts",
    "reservation_conflict",
    "order_contracts_limit",
    "order_cost_limit",
    "market_exposure_limit",
    "event_exposure_limit",
    "group_exposure_limit",
    "portfolio_exposure_limit",
    "insufficient_buying_power",
    "daily_loss_stop",
    "drawdown_stop",
    "order_rate_limit",
    "open_orders_limit",
)
_INT_LIMITS = ("max_orders_per_minute", "max_open_orders")
_BOOL_LIMITS = ("auto_kill_on_loss_stop",)


class LimitsIncomplete(ValueError):
    def __init__(self, missing):
        self.missing = tuple(missing)
        super().__init__("risk limits unset: " + ", ".join(self.missing))


@dataclass(frozen=True)
class RiskLimits:
    """All fields optional; `None` means unset. Money fields are dollars."""

    max_order_contracts: object = None
    max_order_cost: object = None
    max_market_exposure: object = None
    max_event_exposure: object = None
    max_group_exposure: object = None
    max_portfolio_exposure: object = None
    min_cash_buffer: object = None
    daily_loss_limit: object = None
    max_drawdown: object = None
    max_data_age_seconds: object = None
    max_forecast_age_seconds: object = None
    max_orders_per_minute: int | None = None
    max_open_orders: int | None = None
    max_price_deviation: object = None
    min_edge: object = None
    kelly_fraction: object = None
    kelly_portfolio_cap: object = None
    auto_kill_on_loss_stop: bool | None = None

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            if f.name in _INT_LIMITS:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{f.name} must be a nonnegative integer")
            elif f.name in _BOOL_LIMITS:
                if not isinstance(value, bool):
                    raise TypeError(f"{f.name} must be a bool")
            else:
                object.__setattr__(self, f.name, nonnegative(value, f.name))
        if self.kelly_fraction is not None and self.kelly_fraction > ONE:
            raise ValueError("kelly_fraction must be within [0, 1]")

    def missing(self):
        return tuple(f.name for f in fields(self) if getattr(self, f.name) is None)

    def is_complete(self):
        return not self.missing()

    def require_complete(self):
        """Raise `LimitsIncomplete` unless every limit is set (the live requirement)."""
        if self.missing():
            raise LimitsIncomplete(self.missing())
        return self

    def to_dict(self):
        return plain(self)

    def digest(self):
        return digest(self.to_dict())


def limits_from_dict(data):
    return RiskLimits(**data)


SIMULATION_BANKROLL = Decimal(1000)
SIMULATION_ASSUMPTIONS = (
    "Risk limits are SIMULATION ASSUMPTIONS for paper runs, not recommendations and not "
    "live settings (live requires every limit to be set explicitly).",
    "kelly_fraction 0.10; Kelly portfolio cap 0.25 of bankroll; per-market, per-event and "
    "per-group exposure 5% of bankroll; portfolio exposure 25%; order cost 5%.",
    "Data age <= 300 s, forecast age <= 3600 s, <= 10 orders/minute, <= 20 open orders.",
    "Daily loss stop 5% and drawdown stop 15% of bankroll on conservative equity, with "
    "automatic kill switch; cash buffer 5%; minimum edge 0.02; price deviation <= 0.10.",
)


def simulation_defaults(bankroll=SIMULATION_BANKROLL):
    """The labelled simulation-assumption limits, with money limits scaled to `bankroll`."""
    b = positive(bankroll, "bankroll")
    return RiskLimits(
        max_order_contracts=Decimal(1000),
        max_order_cost=b * Decimal("0.05"),
        max_market_exposure=b * Decimal("0.05"),
        max_event_exposure=b * Decimal("0.05"),
        max_group_exposure=b * Decimal("0.05"),
        max_portfolio_exposure=b * Decimal("0.25"),
        min_cash_buffer=b * Decimal("0.05"),
        daily_loss_limit=b * Decimal("0.05"),
        max_drawdown=b * Decimal("0.15"),
        max_data_age_seconds=Decimal(300),
        max_forecast_age_seconds=Decimal(3600),
        max_orders_per_minute=10,
        max_open_orders=20,
        max_price_deviation=Decimal("0.10"),
        min_edge=Decimal("0.02"),
        kelly_fraction=Decimal("0.10"),
        kelly_portfolio_cap=Decimal("0.25"),
        auto_kill_on_loss_stop=True,
    )


SIMULATION_DEFAULTS = simulation_defaults()


def _checks(value):
    return tuple(frozen_mapping(c, "check") for c in value)


@dataclass(frozen=True)
class Approval:
    """Risk approval for one intent; `approval_id` is the digest of its content."""

    intent_id: str
    quantity: object
    limit_price: object
    qty_step: object
    fee_bound_per_step: object
    limits_version: str
    state_digest: str
    checks: tuple
    sizing: object
    created_at: datetime
    approval_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "quantity", positive(self.quantity, "quantity"))
        object.__setattr__(self, "limit_price", price(self.limit_price, "limit_price"))
        object.__setattr__(self, "qty_step", positive(self.qty_step, "qty_step"))
        object.__setattr__(
            self, "fee_bound_per_step", nonnegative(self.fee_bound_per_step, "fee_bound")
        )
        if not on_grid(self.quantity, self.qty_step):
            raise ValueError("approved quantity must lie on the step grid")
        if not isinstance(self.limits_version, str) or not self.limits_version:
            raise ValueError("an approval must cite a limits version")
        object.__setattr__(self, "checks", _checks(self.checks))
        if not all(c["passed"] for c in self.checks):
            raise ValueError("an approval cannot contain a failed check")
        object.__setattr__(self, "sizing", frozen_mapping(self.sizing, "sizing"))
        object.__setattr__(self, "created_at", utc(self.created_at, "created_at"))
        expected = digest({k: v for k, v in plain(self).items() if k != "approval_id"})
        if self.approval_id and self.approval_id != expected:
            raise ValueError("approval_id does not match the approval content")
        object.__setattr__(self, "approval_id", expected)

    def to_dict(self):
        return plain(self)


@dataclass(frozen=True)
class Rejection:
    intent_id: str
    reasons: tuple
    checks: tuple
    created_at: datetime
    limits_version: str | None = None
    state_digest: str = ""
    engage_kill_switch: bool = False
    rejection_id: str = ""

    def __post_init__(self):
        reasons = tuple(self.reasons)
        if not reasons or not all(r in REASONS for r in reasons):
            raise ValueError("a rejection needs known reason codes")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "checks", _checks(self.checks))
        object.__setattr__(self, "created_at", utc(self.created_at, "created_at"))
        if not isinstance(self.engage_kill_switch, bool):
            raise TypeError("engage_kill_switch must be a bool")
        expected = digest({k: v for k, v in plain(self).items() if k != "rejection_id"})
        if self.rejection_id and self.rejection_id != expected:
            raise ValueError("rejection_id does not match the rejection content")
        object.__setattr__(self, "rejection_id", expected)

    def to_dict(self):
        return plain(self)


def approval_from_dict(data):
    return Approval(**data)


def rejection_from_dict(data):
    return Rejection(**data)


@dataclass(frozen=True)
class ForecastStatus:
    """Calibrated YES probability for one ticker. `compatible=False` (missing or
    incompatible model) means unavailable: there is never a fallback forecast."""

    ticker: str
    probability: object
    computed_at: datetime
    model_id: str
    compatible: bool = True
    standard_error: object = None

    def __post_init__(self):
        object.__setattr__(self, "probability", price(self.probability, "probability"))
        object.__setattr__(self, "computed_at", utc(self.computed_at, "computed_at"))
        se = opt_dec(self.standard_error, "standard_error")
        if se is not None and se < 0:
            raise ValueError("standard_error must be nonnegative")
        object.__setattr__(self, "standard_error", se)


@dataclass(frozen=True)
class RiskContext:
    environment: str
    now: datetime
    projection: object
    limits: RiskLimits | None
    limits_version: str | None
    kill_switch_engaged: bool
    fee_schedule: object
    market: object = None
    snapshot: object = None
    forecast: ForecastStatus | None = None
    marks: dict = field(default_factory=dict)
    markets: dict = field(default_factory=dict)
    equity_history: tuple = ()
    unknown_state: tuple = ()

    def __post_init__(self):
        if self.environment not in ENVIRONMENTS:
            raise ValueError("unknown environment")
        object.__setattr__(self, "now", utc(self.now, "now"))


def exposures(projection, clusters):
    """Maximum-loss exposure by market, event, group (cluster) and in total."""
    market, event, group = {}, {}, {}
    total = ZERO

    def add(ticker, amount):
        nonlocal total
        ev = projection.event_of.get(ticker, ticker)
        cl = clusters.get(ev, ev)
        market[ticker] = market.get(ticker, ZERO) + amount
        event[ev] = event.get(ev, ZERO) + amount
        group[cl] = group.get(cl, ZERO) + amount
        total += amount

    port = projection.portfolio
    for ticker, h in port.holdings.items():
        add(ticker, h.yes_cost + h.no_cost)
    for coid, (cash, _) in port.order_reservations.items():
        if cash:
            add(projection.orders[coid].order.ticker, cash)
    return market, event, group, total


def equity_stops(history, now, equity):
    """(daily loss since the UTC day start, drawdown from the peak) on given equity."""
    points = sorted((utc(t), dec(e)) for t, e in history if utc(t) <= now)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    before = [e for t, e in points if t < day_start]
    today = [e for t, e in points if t >= day_start]
    start = before[-1] if before else (today[0] if today else equity)
    peak = max([e for _, e in points] + [equity])
    return start - equity, peak - equity


class RiskEngine:
    """Pure evaluator. Configuration (clipping, probability haircut, z, dependence
    predicate) is fixed at construction; limits arrive only through the context."""

    def __init__(
        self,
        environment,
        *,
        clip_to_limits=False,
        probability_haircut=ZERO,
        z=ZERO,
        dependent=same_team_same_date,
    ):
        if environment not in ENVIRONMENTS:
            raise ValueError("unknown environment")
        self.environment = environment
        self.clip_to_limits = bool(clip_to_limits)
        self.probability_haircut = nonnegative(probability_haircut, "probability_haircut")
        self.z = nonnegative(z, "z")
        self.dependent = dependent

    @in_exact
    def evaluate(self, intent, ctx):
        if ctx.environment != self.environment:
            raise ValueError("context environment differs from the engine environment")
        checks, reasons, clips = [], [], []

        def check(name, passed, value=None, limit=None):
            checks.append(
                {
                    "check": name,
                    "value": value if isinstance(value, (str, type(None))) else text(value),
                    "limit": limit if isinstance(limit, (str, type(None))) else text(limit),
                    "passed": bool(passed),
                }
            )
            if not passed:
                reasons.append(name)

        limits = ctx.limits or RiskLimits()
        projection, port = ctx.projection, ctx.projection.portfolio
        increasing = intent.action == "buy"
        market, snap, forecast = ctx.market, ctx.snapshot, ctx.forecast
        now = ctx.now

        # 1. kill switch (risk-increasing intents only)
        check(
            "kill_switch_engaged",
            not (increasing and ctx.kill_switch_engaged),
            "engaged" if ctx.kill_switch_engaged else "disengaged",
        )
        # 2. configuration and state
        # Live needs every limit; paper records unset limits as not enforced. Without a
        # recorded limits version nothing can be approved in either environment.
        missing = limits.missing() if ctx.limits is not None else ("all",)
        check(
            "limits_unset",
            ctx.limits_version is not None and (not missing or self.environment == "paper"),
            ",".join(missing) or None,
            "all_required" if self.environment == "live" else "unset_not_enforced",
        )
        flags = set(ctx.unknown_state) | set(projection.unknown_flags())
        if self.environment == "live" and not ctx.equity_history:
            flags.add("equity_history_missing")  # loss stops cannot be evaluated: fail closed
        unknown = tuple(sorted(flags))
        check("state_unknown", not (increasing and unknown), ",".join(unknown) or None)
        market_ok = (
            market is not None
            and market.ticker == intent.ticker
            and market.event_ticker == intent.event_ticker
        )
        check("market_unknown", market_ok, intent.ticker)
        is_open = market_ok and market.is_open(now) and intent.ticker not in port.settlements
        check("market_not_open", is_open, market.status if market_ok else None)
        # 3. freshness
        age = None
        if snap is not None and snap.ticker == intent.ticker:
            age = seconds(now - snap.as_of)
        max_age = limits.max_data_age_seconds
        check(
            "data_stale",
            age is not None and age >= 0 and (max_age is None or age <= max_age),
            age,
            max_age,
        )
        f_age = None
        usable = forecast is not None and forecast.compatible and forecast.ticker == intent.ticker
        ref_probability = intent.forecast_ref.get("probability")
        if usable and ref_probability is not None and ref_probability != text(forecast.probability):
            usable = False
        if usable:
            f_age = seconds(now - forecast.computed_at)
        max_f_age = limits.max_forecast_age_seconds
        # A forecast is required to add risk (edge and sizing), not to reduce it.
        fresh = f_age is None or (f_age >= 0 and (max_f_age is None or f_age <= max_f_age))
        check("forecast_stale", fresh or not increasing, f_age, max_f_age)
        check(
            "forecast_unavailable",
            usable or not increasing,
            forecast.model_id if forecast else None,
        )
        # 4. price
        p = intent.limit_price
        tick = market.tick if market_ok else None
        check("price_off_grid", tick is not None and on_grid(p, tick), p, tick)
        in_bounds = market_ok and market.min_price <= p <= market.max_price
        check(
            "price_out_of_bounds",
            in_bounds,
            p,
            f"{text(market.min_price)}..{text(market.max_price)}" if market_ok else None,
        )
        mid = snap.mid(intent.side) if age is not None else None
        deviation = abs(p - mid) if mid is not None else None
        max_dev = limits.max_price_deviation
        check(
            "price_deviation",
            max_dev is None or (deviation is not None and deviation <= max_dev),
            deviation,
            max_dev,
        )
        # 5. edge (buys)
        step = market.qty_step if market_ok else ONE
        liquidity = "maker" if intent.post_only else "taker"
        fee_pc = ctx.fee_schedule.fee_per_contract(p, liquidity, step)
        q_side = None
        if usable:
            q_side = forecast.probability if intent.side == "yes" else ONE - forecast.probability
        edge = q_side - p - fee_pc if q_side is not None else None
        if increasing:
            min_edge = limits.min_edge
            check(
                "edge_below_minimum",
                edge is not None and (min_edge is None or edge >= min_edge),
                edge,
                min_edge,
            )
        else:
            check("edge_below_minimum", True, "reduce_only")
        # 6. sizing
        fee_bound = ctx.fee_schedule.fee_bound_per_step(
            p, intent.action, step, liquidity if intent.post_only else None
        )
        clusters = correlation_groups(
            [m for m in {**ctx.markets, **({market.ticker: market} if market_ok else {})}.values()],
            self.dependent,
        )
        by_market, by_event, by_group, total = exposures(projection, clusters)
        if increasing:
            sizing = self._size_buy(
                intent, ctx, q_side, fee_pc, step, limits, clusters, by_group, total
            )
            qty = sizing["quantity"]
            check("size_zero", qty > 0, qty)
            reserved_opposite = port.reserved_contracts.get((intent.ticker, opposite(intent.side)))
            check("reservation_conflict", not reserved_opposite, reserved_opposite)
        else:
            available = port.available(intent.ticker, intent.side)
            requested = intent.max_quantity if intent.max_quantity is not None else available
            qty = floor_to(max(ZERO, min(requested, available)), step)
            sizing = {"method": "reduce_only", "available": text(available), "quantity": qty}
            check("size_zero", qty > 0, qty)
            check("insufficient_contracts", requested <= available, requested, available)
        # 7-8. order and exposure limits (clip down when configured)
        per_step = step * p + fee_bound if increasing else ZERO

        def cap(name, limit, current_amount):
            nonlocal qty
            if limit is None:
                check(name, True, None, "unset")
                return
            if not increasing:
                check(name, True, "reduce_only", limit)
                return
            room = limit - current_amount
            # Exact integer division: lots that fit in the remaining room.
            max_qty = (room // per_step) * step if room > 0 and per_step > 0 else ZERO
            if qty <= max_qty:
                check(name, True, current_amount + steps(qty, step) * per_step, limit)
            elif self.clip_to_limits and max_qty > 0:
                clips.append({"check": name, "from": text(qty), "to": text(max_qty)})
                qty = max_qty
                check(name, True, current_amount + steps(qty, step) * per_step, limit)
            else:
                check(name, False, current_amount + steps(qty, step) * per_step, limit)

        if limits.max_order_contracts is None:
            check("order_contracts_limit", True, qty, "unset")
        elif qty <= limits.max_order_contracts:
            check("order_contracts_limit", True, qty, limits.max_order_contracts)
        elif self.clip_to_limits and floor_to(limits.max_order_contracts, step) > 0:
            clipped = floor_to(limits.max_order_contracts, step)
            clips.append({"check": "order_contracts_limit", "from": text(qty), "to": text(clipped)})
            qty = clipped
            check("order_contracts_limit", True, qty, limits.max_order_contracts)
        else:
            check("order_contracts_limit", False, qty, limits.max_order_contracts)
        cap("order_cost_limit", limits.max_order_cost, ZERO)
        event_key = intent.event_ticker
        cluster = clusters.get(event_key, event_key)
        cap("market_exposure_limit", limits.max_market_exposure, by_market.get(intent.ticker, ZERO))
        cap("event_exposure_limit", limits.max_event_exposure, by_event.get(event_key, ZERO))
        cap("group_exposure_limit", limits.max_group_exposure, by_group.get(cluster, ZERO))
        cap("portfolio_exposure_limit", limits.max_portfolio_exposure, total)
        # 9. buying power
        lots = steps(qty, step) if qty > 0 else ZERO
        if increasing:
            new_cash = qty * p + lots * fee_bound
        else:
            new_cash = max(ZERO, lots * fee_bound - qty * p)
        # The cash buffer protects against adding risk; a reduce-only sell needs only its
        # own fee shortfall funded.
        buffer = (limits.min_cash_buffer or ZERO) if increasing else ZERO
        required = new_cash + port.reserved_cash + buffer
        check("insufficient_buying_power", required <= port.cash, required, port.cash)
        # 10. loss stops (risk-increasing only)
        equity = port.equity(ctx.marks)
        daily_loss, drawdown = equity_stops(ctx.equity_history, now, equity)
        loss_limit, dd_limit = limits.daily_loss_limit, limits.max_drawdown
        loss_hit = loss_limit is not None and daily_loss >= loss_limit
        dd_hit = dd_limit is not None and drawdown >= dd_limit
        check("daily_loss_stop", not (increasing and loss_hit), daily_loss, loss_limit)
        check("drawdown_stop", not (increasing and dd_hit), drawdown, dd_limit)
        # 11. order rate and open orders
        window = now - timedelta(seconds=60)
        recent = sum(1 for s in projection.orders.values() if window < s.order.created_at <= now)
        rate = limits.max_orders_per_minute
        check("order_rate_limit", rate is None or recent < rate, recent, rate)
        open_count = len(projection.active_orders)
        max_open = limits.max_open_orders
        check("open_orders_limit", max_open is None or open_count < max_open, open_count, max_open)

        sizing = {
            **{k: (text(v) if isinstance(v, Decimal) else v) for k, v in sizing.items()},
            "clips": clips,
            "approved_quantity": text(qty),
        }
        state = self.state_digest(ctx, unknown)
        if reasons:
            engage = bool((loss_hit or dd_hit) and increasing and limits.auto_kill_on_loss_stop)
            return Rejection(
                intent.intent_id,
                tuple(reasons),
                tuple(checks),
                now,
                ctx.limits_version,
                state,
                engage,
            )
        return Approval(
            intent.intent_id,
            qty,
            p,
            step,
            fee_bound,
            ctx.limits_version,
            state,
            tuple(checks),
            sizing,
            now,
        )

    def _size_buy(self, intent, ctx, q_side, fee_pc, step, limits, clusters, by_group, total):
        port, projection = ctx.projection.portfolio, ctx.projection
        cluster = clusters.get(intent.event_ticker, intent.event_ticker)
        directions = set()
        for ticker, h in port.holdings.items():
            ev = projection.event_of.get(ticker, ticker)
            if clusters.get(ev, ev) == cluster:
                for side in ("yes", "no"):
                    if h.quantity(side) > 0:
                        directions.add((ticker, side))
        current = port.holding(intent.ticker).cost(intent.side)
        for state in projection.active_orders:
            order = state.order
            ev = projection.event_of.get(order.ticker, order.ticker)
            if order.action == "buy" and clusters.get(ev, ev) == cluster:
                directions.add((order.ticker, order.side))
                if (order.ticker, order.side) == (intent.ticker, intent.side):
                    current += port.order_reservations.get(order.client_order_id, (ZERO,))[0]
        conflict = bool(directions - {(intent.ticker, intent.side)})
        if q_side is None:
            return {"method": "kelly", "quantity": ZERO, "notes": ["forecast_unavailable"]}
        if limits.kelly_fraction is None:
            qty = floor_to(intent.max_quantity, step) if intent.max_quantity else ZERO
            notes = ["kelly_not_configured"]
            if conflict:
                qty, notes = ZERO, notes + ["group_direction_conflict_reduce_first"]
            return {"method": "max_quantity", "quantity": qty, "notes": notes}
        bankroll = port.equity(ctx.marks) - port.reserved_cash
        se = ctx.forecast.standard_error if ctx.forecast is not None else None
        existing = total / bankroll if bankroll > 0 else ZERO
        result = kelly.size(
            probability=q_side,
            limit_price=intent.limit_price,
            fee_per_contract=fee_pc,
            bankroll=bankroll,
            kelly_fraction=limits.kelly_fraction,
            qty_step=step,
            haircut=self.probability_haircut,
            standard_error=se,
            z=self.z,
            current_exposure=current,
            existing_fraction_total=existing,
            portfolio_cap=limits.kelly_portfolio_cap,
            max_quantity=intent.max_quantity,
        )
        sizing = {"method": "kelly", "cluster": cluster, **result.to_dict()}
        sizing["quantity"] = result.quantity
        if conflict:
            sizing["quantity"] = ZERO
            sizing["notes"] = list(result.notes) + ["group_direction_conflict_reduce_first"]
        return sizing

    @staticmethod
    def state_digest(ctx, unknown):
        port = ctx.projection.portfolio
        return digest(
            {
                "cash": port.cash,
                "reserved_cash": port.reserved_cash,
                "holdings": {t: plain(h) for t, h in port.holdings.items()},
                "active": [
                    (s.order.client_order_id, s.status, s.remaining)
                    for s in ctx.projection.active_orders
                ],
                "kill_switch": ctx.kill_switch_engaged,
                "limits_version": ctx.limits_version,
                "unknown": list(unknown),
            }
        )
