"""Positions, cash, reservations and PnL as a deterministic replay of ledger records.

Holdings per ticker are `yes_qty/yes_cost` and `no_qty/no_cost`; cost includes the fees
of the opening fills. Kalshi-style netting: after a buy, `m = min(yes_qty, no_qty)`
pairs are closed immediately for $1 each, both cost bases reduced proportionally
(average cost) and the difference realized. A sell reduces its side and may never
exceed the holding. Settlements apply after all fills of their ticker (fills on a
settled ticker are refused by the ledger); a correction supersedes a settlement and the
replay recomputes everything from scratch, which reverses the old effect.

Cash: buy -= price x qty + fee; sell += price x qty - fee; netting += $1 per pair;
settlement += value x yes_qty + (1 - value) x no_qty. `realized` is net of all fees,
so `cash + remaining cost = net deposits + realized` exactly.
"""

from collections import defaultdict
from dataclasses import dataclass

from .money import (
    ONE,
    ZERO,
    dec,
    fixed,
    in_exact,
    plain,
    price,
    steps,
    utc,
)
from .orders import SIDES, order_state

RESULTS = ("yes", "no", "fair")
CASH_KINDS = ("deposit", "withdrawal", "adjustment", "balance_observed")
CASH_KINDS_BY_ENVIRONMENT = {
    "paper": ("deposit", "withdrawal", "adjustment"),
    "live": ("adjustment", "balance_observed"),
}


class PortfolioError(ValueError):
    """Records that cannot be replayed (e.g. a sell larger than the holding)."""


@dataclass(frozen=True)
class Settlement:
    settlement_id: str
    ticker: str
    result: str
    settlement_value: object
    source_ref: str
    effective_at: object
    supersedes_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.settlement_id, str) or not self.settlement_id:
            raise ValueError("settlement_id is required")
        if self.result not in RESULTS:
            raise ValueError(f"unknown settlement result {self.result!r}")
        value = price(self.settlement_value, "settlement_value")
        if (self.result == "yes" and value != ONE) or (self.result == "no" and value != ZERO):
            raise ValueError("settlement value contradicts the result")
        if not isinstance(self.source_ref, str) or not self.source_ref:
            raise ValueError("settlement source_ref is required")
        if self.supersedes_id == self.settlement_id:
            raise ValueError("a settlement cannot supersede itself")
        object.__setattr__(self, "settlement_value", value)
        object.__setattr__(self, "effective_at", utc(self.effective_at, "effective_at"))

    def to_dict(self):
        return plain(self)


@dataclass(frozen=True)
class CashEvent:
    event_id: str
    kind: str
    amount: object
    at: object
    note: str = ""

    def __post_init__(self):
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("cash event_id is required")
        if self.kind not in CASH_KINDS:
            raise ValueError(f"unknown cash event kind {self.kind!r}")
        amount = dec(self.amount, "amount")
        if self.kind in ("deposit", "withdrawal") and amount <= 0:
            raise ValueError("deposits and withdrawals must be positive")
        if self.kind == "adjustment" and amount == 0:
            raise ValueError("an adjustment must be nonzero")
        if self.kind == "balance_observed" and amount < 0:
            raise ValueError("an observed balance cannot be negative")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "at", utc(self.at, "at"))

    def to_dict(self):
        return plain(self)


@dataclass(frozen=True)
class Holding:
    ticker: str
    yes_qty: object = ZERO
    yes_cost: object = ZERO
    no_qty: object = ZERO
    no_cost: object = ZERO

    def quantity(self, side):
        return self.yes_qty if side == "yes" else self.no_qty

    def cost(self, side):
        return self.yes_cost if side == "yes" else self.no_cost

    @property
    def flat(self):
        return not (self.yes_qty or self.no_qty or self.yes_cost or self.no_cost)


@dataclass(frozen=True)
class Totals:
    deposits: object = ZERO
    withdrawals: object = ZERO
    adjustments: object = ZERO
    purchases: object = ZERO
    sales: object = ZERO
    fees: object = ZERO
    netting_credits: object = ZERO
    settlement_payouts: object = ZERO


@dataclass(frozen=True)
class Portfolio:
    cash: object
    holdings: dict
    realized: object
    totals: Totals
    settlements: dict
    reserved_cash: object
    reserved_contracts: dict
    order_reservations: dict

    def holding(self, ticker):
        return self.holdings.get(ticker) or Holding(ticker)

    @property
    def net_deposits(self):
        t = self.totals
        return t.deposits - t.withdrawals + t.adjustments

    @property
    def remaining_cost(self):
        return sum((h.yes_cost + h.no_cost for h in self.holdings.values()), ZERO)

    def available(self, ticker, side):
        """Held contracts of one side not reserved by open sell orders."""
        held = self.holding(ticker).quantity(side)
        return held - self.reserved_contracts.get((ticker, side), ZERO)

    @in_exact
    def market_value(self, marks, mode="conservative"):
        """(value, tickers lacking a usable mark). Conservative = best bid of the held
        side (missing bid -> 0); mid = YES mid (missing -> conservative value)."""
        total, missing = ZERO, []
        for ticker in sorted(self.holdings):
            h = self.holdings[ticker]
            snap = marks.get(ticker) if marks else None
            yes_bid = snap.best_yes_bid if snap is not None else None
            no_bid = snap.best_no_bid if snap is not None else None
            conservative = h.yes_qty * (yes_bid or ZERO) + h.no_qty * (no_bid or ZERO)
            if (h.yes_qty and yes_bid is None) or (h.no_qty and no_bid is None):
                missing.append(ticker)
            if mode == "conservative":
                total += conservative
                continue
            mid = snap.yes_mid if snap is not None else None
            if mid is None:
                if ticker not in missing:
                    missing.append(ticker)
                total += conservative
            else:
                total += h.yes_qty * mid + h.no_qty * (ONE - mid)
        return total, tuple(missing)

    def equity(self, marks, mode="conservative"):
        return self.cash + self.market_value(marks, mode)[0]

    def unrealized(self, marks, mode="conservative"):
        return self.market_value(marks, mode)[0] - self.remaining_cost


def effective_settlements(settlements):
    """Chain heads by ticker. Each settlement is superseded at most once, only by one
    for the same ticker; two independent heads for one ticker are refused."""
    by_id = {s.settlement_id: s for s in settlements}
    superseded = {}
    for s in settlements:
        if s.supersedes_id is None:
            continue
        old = by_id.get(s.supersedes_id)
        if old is None or old.ticker != s.ticker:
            raise PortfolioError("a correction must supersede a settlement of its ticker")
        if s.supersedes_id in superseded:
            raise PortfolioError("a settlement can be superseded only once")
        superseded[s.supersedes_id] = s.settlement_id
    heads = {}
    for s in settlements:
        if s.settlement_id in superseded:
            continue
        if s.ticker in heads:
            raise PortfolioError(f"two effective settlements for {s.ticker}")
        heads[s.ticker] = s
    return heads


@in_exact
def reservation(state):
    """(cash, contracts) held by one order state; zero once terminal.

    Buy: remaining x limit + lots x fee bound. Sell: contracts plus any fee shortfall
    (lots x bound - remaining x limit, floored at 0), since sells fill at >= limit.
    """
    remaining = state.remaining
    if remaining <= 0:
        return ZERO, ZERO
    order = state.order
    fees = steps(remaining, order.qty_step) * order.fee_bound_per_step
    notional = remaining * order.limit_price
    if order.action == "buy":
        return notional + fees, ZERO
    return max(ZERO, fees - notional), remaining


@in_exact
def replay(fills, settlements=(), cash_events=(), order_states=()):
    """Project the portfolio from fills (ledger order), settlements and cash events."""
    effective = effective_settlements(settlements)
    positions = {}
    totals = defaultdict(lambda: ZERO)
    cash = realized = ZERO
    for event in cash_events:
        if event.kind == "deposit":
            cash += event.amount
            totals["deposits"] += event.amount
        elif event.kind == "withdrawal":
            cash -= event.amount
            totals["withdrawals"] += event.amount
        elif event.kind == "adjustment":
            cash += event.amount
            totals["adjustments"] += event.amount
    for fill in fills:
        p = positions.setdefault(fill.ticker, [ZERO, ZERO, ZERO, ZERO])
        i = 0 if fill.side == "yes" else 2
        gross = fill.price * fill.quantity
        totals["fees"] += fill.fee
        if fill.action == "buy":
            cash -= gross + fill.fee
            totals["purchases"] += gross
            p[i] += fill.quantity
            p[i + 1] += gross + fill.fee
            pairs = min(p[0], p[2])
            if pairs > 0:
                cost_yes = p[1] if pairs == p[0] else fixed(p[1] * pairs / p[0])
                cost_no = p[3] if pairs == p[2] else fixed(p[3] * pairs / p[2])
                p[0], p[1], p[2], p[3] = p[0] - pairs, p[1] - cost_yes, p[2] - pairs, p[3] - cost_no
                cash += pairs
                totals["netting_credits"] += pairs
                realized += pairs - cost_yes - cost_no
        else:
            held = p[i]
            if fill.quantity > held:
                raise PortfolioError(f"sell of {fill.quantity} exceeds holding {held}")
            removed = p[i + 1] if fill.quantity == held else fixed(p[i + 1] * fill.quantity / held)
            p[i] -= fill.quantity
            p[i + 1] -= removed
            cash += gross - fill.fee
            totals["sales"] += gross
            realized += gross - fill.fee - removed
    for ticker in sorted(effective):
        p = positions.get(ticker)
        if not p:
            continue
        value = effective[ticker].settlement_value
        payout = p[0] * value + p[2] * (ONE - value)
        cash += payout
        totals["settlement_payouts"] += payout
        realized += payout - p[1] - p[3]
        positions[ticker] = [ZERO, ZERO, ZERO, ZERO]
    holdings = {t: Holding(t, *p) for t, p in sorted(positions.items()) if any(v != 0 for v in p)}
    per_order, reserved_cash, contracts = {}, ZERO, defaultdict(lambda: ZERO)
    for state in order_states:
        held_cash, held_contracts = reservation(state)
        if held_cash or held_contracts:
            per_order[state.order.client_order_id] = (held_cash, held_contracts)
            reserved_cash += held_cash
            if held_contracts:
                contracts[(state.order.ticker, state.order.side)] += held_contracts
    return Portfolio(
        cash=cash,
        holdings=holdings,
        realized=realized,
        totals=Totals(**totals),
        settlements=effective,
        reserved_cash=reserved_cash,
        reserved_contracts=dict(contracts),
        order_reservations=per_order,
    )


@in_exact
def check(portfolio, *, fills, settlements, cash_events, order_states, environment):
    """§7 invariants, cash and netting recomputed independently of the replay."""
    failures = []
    flows = ZERO
    for event in cash_events:
        flows += {"deposit": ONE, "withdrawal": -ONE, "adjustment": ONE}.get(
            event.kind, ZERO
        ) * event.amount
    traded = defaultdict(lambda: ZERO)
    for fill in fills:
        gross = fill.price * fill.quantity
        flows += (-gross if fill.action == "buy" else gross) - fill.fee
        traded[(fill.ticker, fill.side, fill.action)] += fill.quantity
    netting = payouts = ZERO
    effective = effective_settlements(settlements)
    for ticker in sorted({key[0] for key in traded}):
        yes_net = traded[(ticker, "yes", "buy")] - traded[(ticker, "yes", "sell")]
        no_net = traded[(ticker, "no", "buy")] - traded[(ticker, "no", "sell")]
        net = yes_net - no_net
        netting += yes_net - max(net, ZERO)
        if ticker in effective:
            value = effective[ticker].settlement_value
            payouts += max(net, ZERO) * value + max(-net, ZERO) * (ONE - value)
    expected = flows + netting + payouts
    if expected != portfolio.cash:
        failures.append(f"cash {portfolio.cash} != independent reconstruction {expected}")
    if netting != portfolio.totals.netting_credits:
        failures.append("netting credits disagree with the holdings identity")
    if payouts != portfolio.totals.settlement_payouts:
        failures.append("settlement payouts disagree with the holdings identity")
    lhs = portfolio.cash + portfolio.remaining_cost
    if lhs != portfolio.net_deposits + portfolio.realized:
        failures.append("realized + unrealized does not reconcile to equity - net deposits")
    for h in portfolio.holdings.values():
        if min(h.yes_qty, h.no_qty, h.yes_cost, h.no_cost) < 0:
            failures.append(f"negative holding in {h.ticker}")
        if h.yes_qty and h.no_qty:
            failures.append(f"un-netted YES and NO holdings in {h.ticker}")
    if environment == "paper" and portfolio.cash < 0:
        failures.append("negative cash in a paper ledger")
    if environment == "paper" and portfolio.reserved_cash > portfolio.cash:
        failures.append("reservations exceed cash in a paper ledger")
    reserved, contracts = ZERO, defaultdict(lambda: ZERO)
    for state in order_states:
        if state.filled_quantity > state.order.quantity or state.inconsistent:
            failures.append(f"order {state.order.client_order_id} is inconsistent")
        held = portfolio.order_reservations.get(state.order.client_order_id)
        if not state.active and held:
            failures.append(f"terminal order {state.order.client_order_id} holds a reservation")
        if state.active:
            remaining = state.order.quantity - state.filled_quantity
            lots = steps(max(remaining, ZERO), state.order.qty_step)
            fee = lots * state.order.fee_bound_per_step
            notional = max(remaining, ZERO) * state.order.limit_price
            if state.order.action == "buy":
                reserved += notional + fee
            else:
                reserved += max(ZERO, fee - notional)
                contracts[(state.order.ticker, state.order.side)] += max(remaining, ZERO)
    if reserved != portfolio.reserved_cash or dict(contracts) != portfolio.reserved_contracts:
        failures.append("reservations differ from the sum of open-order requirements")
    if portfolio.reserved_cash < 0:
        failures.append("negative reservation")
    for (ticker, side), qty in portfolio.reserved_contracts.items():
        if side not in SIDES or qty > portfolio.holding(ticker).quantity(side):
            failures.append(f"sell reservations exceed the {side} holding in {ticker}")
    return failures


@dataclass(frozen=True)
class Projection:
    """Ledger state used by risk: order states, portfolio and the event of each ticker."""

    environment: str
    orders: dict
    portfolio: Portfolio
    event_of: dict

    @property
    def active_orders(self):
        return [s for _, s in sorted(self.orders.items()) if s.active]

    def unknown_flags(self):
        """Required state that is not known: unresolved submissions or inconsistencies."""
        flags = []
        for coid, state in sorted(self.orders.items()):
            if state.status == "unknown":
                flags.append(f"order_unknown:{coid}")
            elif state.status == "pending_submit" and not state.submit_outcome:
                flags.append(f"submission_unresolved:{coid}")
            if state.inconsistent:
                flags.append(f"order_inconsistent:{coid}")
        return tuple(flags)


def project(environment, *, intents, orders, events, fills, settlements, cash_events):
    """Full projection from ledger records (each list in ledger order)."""
    by_order_events, by_order_fills = defaultdict(list), defaultdict(list)
    for event in events:
        by_order_events[event.client_order_id].append(event)
    for fill in fills:
        by_order_fills[fill.client_order_id].append(fill)
    states = {
        o.client_order_id: order_state(
            o, by_order_events[o.client_order_id], by_order_fills[o.client_order_id]
        )
        for o in orders
    }
    portfolio = replay(fills, settlements, cash_events, list(states.values()))
    # Only intents that became orders (approved, so checked against market metadata)
    # define a ticker's event; a rejected intent with a wrong event cannot poison it.
    ordered = {o.intent_id for o in orders}
    event_of = {}
    for intent in intents:
        if intent.intent_id in ordered:
            event_of.setdefault(intent.ticker, intent.event_ticker)
    return Projection(environment, states, portfolio, event_of)
