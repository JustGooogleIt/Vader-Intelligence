"""Fractional Kelly sizing for buying one side of a $1 binary contract.

With all-in per-contract cost `c = p + fee_per_contract(p)` and probability `q` that
the bought side wins, the full-Kelly bankroll fraction is `f* = (q - c) / (1 - c)` when
`q > c`, else 0. Sizing uses `q_adj = q - haircut - z x standard_error` (floored at 0),
`f = kelly_fraction x f*(q_adj)`, adds only `f x bankroll - current same-side
exposure` (never negative), scales new increments so the portfolio's sum of group
fractions stays within `portfolio_cap`, and buys `floor_to_step(add / c)` contracts.
Invariants: monotone non-decreasing in q, non-increasing in price and fee, zero when
q_adj <= c, never above the cap; proportional scaling preserves ordering.
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, localcontext

from .money import ONE, QUANTUM, ZERO, dec, floor_to, in_exact, nonnegative, plain, positive, price


@in_exact
def full_kelly(probability, cost):
    """`(q - c) / (1 - c)` when q > c, else 0 (also 0 when c >= 1)."""
    q = price(probability, "probability")
    c = nonnegative(cost, "cost")
    if c >= ONE or q <= c:
        return ZERO
    return (q - c) / (ONE - c)


@in_exact
def adjusted_probability(probability, haircut=ZERO, standard_error=None, z=ZERO):
    """Probability reduced by a fixed margin and `z` model standard errors (>= 0)."""
    q = price(probability, "probability")
    margin = nonnegative(haircut, "haircut")
    if standard_error is not None:
        margin += nonnegative(z, "z") * nonnegative(standard_error, "standard_error")
    return max(ZERO, q - margin)


@in_exact
def scale_to_cap(new, existing_total, cap):
    """Scale new fractions so `existing_total + sum(new) <= cap`, proportionally.

    Existing fractions are never changed (and never increased to use the cap). Each
    scaled value is `v x room / total` rounded down to 18 decimals, so the sum never
    exceeds the room and ordering is preserved.
    """
    values = {k: nonnegative(v, "fraction") for k, v in new.items()}
    total = sum(values.values(), ZERO)
    room = max(ZERO, dec(cap, "cap") - nonnegative(existing_total, "existing_total"))
    if total <= room:
        return values
    with localcontext() as ctx:
        ctx.rounding = ROUND_FLOOR
        # Rounded down to the fixed 18-decimal quantum: sums stay exact in any context.
        return {k: (v * room / total).quantize(QUANTUM) for k, v in values.items()}


@dataclass(frozen=True)
class KellySizing:
    probability: object
    adjusted_probability: object
    price: object
    fee_per_contract: object
    cost: object
    full_fraction: object
    kelly_fraction: object
    fraction: object
    bankroll: object
    target_value: object
    current_exposure: object
    add_value: object
    cap_scale: object
    quantity: object
    notes: tuple

    def to_dict(self):
        return plain(self)


@in_exact
def size(
    *,
    probability,
    limit_price,
    fee_per_contract,
    bankroll,
    kelly_fraction,
    qty_step=ONE,
    haircut=ZERO,
    standard_error=None,
    z=ZERO,
    current_exposure=ZERO,
    existing_fraction_total=ZERO,
    portfolio_cap=None,
    max_quantity=None,
):
    """Contracts to buy for one intent (see module docstring for the rule)."""
    p = price(limit_price, "limit_price")
    fee = nonnegative(fee_per_contract, "fee_per_contract")
    step = positive(qty_step, "qty_step")
    fraction_of_kelly = nonnegative(kelly_fraction, "kelly_fraction")
    if fraction_of_kelly > ONE:
        raise ValueError("kelly_fraction above 1 is not fractional Kelly")
    cost = p + fee
    q_adj = adjusted_probability(probability, haircut, standard_error, z)
    f_star = full_kelly(q_adj, cost)
    f = fraction_of_kelly * f_star
    roll = dec(bankroll, "bankroll")
    current = nonnegative(current_exposure, "current_exposure")
    notes = []
    target = add = ZERO
    scale = ONE
    if roll <= 0:
        notes.append("no_bankroll")
    elif f_star == 0:
        notes.append("no_edge_after_costs")
    else:
        target = f * roll
        add = max(ZERO, target - current)
        if add == 0:
            notes.append("target_already_held")
        elif portfolio_cap is not None:
            # One new target: scale_to_cap reduces to min(add, room); computed in value
            # space so rounding cannot break monotonicity.
            existing = nonnegative(existing_fraction_total, "existing_fraction_total")
            room = max(ZERO, dec(portfolio_cap, "portfolio_cap") - existing) * roll
            if add > room:
                notes.append("portfolio_cap_scaled")
                scale = room / add
                add = room
    quantity = floor_to(add / cost, step) if add > 0 and cost > 0 else ZERO
    if max_quantity is not None:
        cap = floor_to(positive(max_quantity, "max_quantity"), step)
        if quantity > cap:
            notes.append("max_quantity_capped")
            quantity = cap
    if add > 0 and quantity == 0:
        notes.append("below_one_step")
    return KellySizing(
        probability=price(probability, "probability"),
        adjusted_probability=q_adj,
        price=p,
        fee_per_contract=fee,
        cost=cost,
        full_fraction=f_star,
        kelly_fraction=fraction_of_kelly,
        fraction=f,
        bankroll=roll,
        target_value=target,
        current_exposure=current,
        add_value=add,
        cap_scale=scale,
        quantity=quantity,
        notes=tuple(notes),
    )
