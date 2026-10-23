"""Fractional Kelly: the spec's hand-verifiable cases and hypothesis properties."""

from decimal import Decimal

from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from vader_intelligence.trading.kelly import adjusted_probability, full_kelly, scale_to_cap, size

D = Decimal


def test_hand_verifiable_cases():
    assert full_kelly(D("0.60"), D("0.50")) == D("0.20")
    s = size(
        probability=D("0.60"),
        limit_price=D("0.50"),
        fee_per_contract=D(0),
        bankroll=D(1000),
        kelly_fraction=D("0.25"),
    )
    assert s.full_fraction == D("0.20") and s.fraction == D("0.05")
    assert s.cost == D("0.50") and s.target_value == D(50) and s.quantity == D(100)
    assert full_kelly(D("0.50"), D("0.50")) == 0
    assert (
        size(
            probability=D("0.50"),
            limit_price=D("0.50"),
            fee_per_contract=D(0),
            bankroll=D(1000),
            kelly_fraction=D("0.25"),
        ).quantity
        == 0
    )
    assert full_kelly(D("0.55"), D("0.52")) == D("0.0625")  # 0.03 / 0.48
    s = size(
        probability=D("0.55"),
        limit_price=D("0.50"),
        fee_per_contract=D("0.02"),
        bankroll=D(1000),
        kelly_fraction=D(1),
    )
    assert s.cost == D("0.52") and s.full_fraction == D("0.0625")
    assert s.quantity == D(120)  # floor(62.5 / 0.52) = 120


def test_haircut_exposure_cap_and_step():
    assert adjusted_probability(D("0.60"), D("0.02"), D("0.05"), D(2)) == D("0.48")
    assert adjusted_probability(D("0.05"), D("0.10")) == 0
    base = dict(
        probability=D("0.60"),
        limit_price=D("0.50"),
        fee_per_contract=D(0),
        bankroll=D(1000),
        kelly_fraction=D("0.25"),
    )
    held = size(**base, current_exposure=D(30))
    assert held.add_value == D(20) and held.quantity == D(40)
    assert size(**base, current_exposure=D(80)).quantity == 0  # never negative / no closing
    capped = size(**base, existing_fraction_total=D("0.22"), portfolio_cap=D("0.25"))
    assert capped.add_value == D(30) and capped.quantity == D(60)
    assert "portfolio_cap_scaled" in capped.notes
    assert size(**base, existing_fraction_total=D("0.30"), portfolio_cap=D("0.25")).quantity == 0
    stepped = size(**base, qty_step=D(7))
    assert stepped.quantity == D(98)
    assert size(**base, max_quantity=D(10)).quantity == D(10)
    assert size(**{**base, "bankroll": D(0)}).quantity == 0


def test_scale_to_cap_is_proportional_and_never_increases_existing():
    out = scale_to_cap({"a": D("0.10"), "b": D("0.30")}, D("0.05"), D("0.25"))
    assert out == {"a": D("0.05"), "b": D("0.15")}
    assert scale_to_cap({"a": D("0.10")}, D("0.30"), D("0.25")) == {"a": D(0)}
    assert scale_to_cap({"a": D("0.01")}, D(0), D("0.25")) == {"a": D("0.01")}


probability = st.integers(1, 999).map(lambda n: D(n) / 1000)
cents = st.integers(1, 99).map(lambda n: D(n) / 100)
fees = st.integers(0, 5).map(lambda n: D(n) / 100)
fractions = st.integers(1, 100).map(lambda n: D(n) / 100)
bankrolls = st.integers(1, 100_000).map(D)


def _qty(q, p, fee, kf, roll, **kw):
    return size(
        probability=q, limit_price=p, fee_per_contract=fee, bankroll=roll, kelly_fraction=kf, **kw
    ).quantity


@settings(max_examples=300, deadline=None)
@given(q1=probability, q2=probability, p=cents, fee=fees, kf=fractions, roll=bankrolls)
def test_monotone_non_decreasing_in_probability(q1, q2, p, fee, kf, roll):
    lo, hi = sorted((q1, q2))
    assert full_kelly(lo, p + fee) <= full_kelly(hi, p + fee)
    assert _qty(lo, p, fee, kf, roll) <= _qty(hi, p, fee, kf, roll)


@settings(max_examples=300, deadline=None)
@given(q=probability, p1=cents, p2=cents, f1=fees, f2=fees, kf=fractions, roll=bankrolls)
def test_non_increasing_in_price_and_fee(q, p1, p2, f1, f2, kf, roll):
    lo_p, hi_p = sorted((p1, p2))
    lo_f, hi_f = sorted((f1, f2))
    assert _qty(q, hi_p, lo_f, kf, roll) <= _qty(q, lo_p, lo_f, kf, roll)
    assert _qty(q, lo_p, hi_f, kf, roll) <= _qty(q, lo_p, lo_f, kf, roll)


@settings(max_examples=300, deadline=None)
@given(q=probability, p=cents, fee=fees, kf=fractions, roll=bankrolls)
def test_zero_when_probability_at_or_below_cost(q, p, fee, kf, roll):
    assume(q <= p + fee)
    assert full_kelly(q, p + fee) == 0 and _qty(q, p, fee, kf, roll) == 0


@example(
    q=D(75) / 1000,
    p=D(1) / 100,
    fee=D(1) / 100,
    kf=D(24) / 100,
    roll=Decimal(22344),
    existing=D(0) / 100,
    cap=D(2) / 100,
)  # found by hypothesis: add / cost rounded up, buying 1e-37 beyond the allowance
@settings(max_examples=300, deadline=None)
@given(
    q=probability,
    p=cents,
    fee=fees,
    kf=fractions,
    roll=bankrolls,
    existing=st.integers(0, 40).map(lambda n: D(n) / 100),
    cap=st.integers(0, 40).map(lambda n: D(n) / 100),
)
def test_never_exceeds_the_portfolio_cap(q, p, fee, kf, roll, existing, cap):
    s = size(
        probability=q,
        limit_price=p,
        fee_per_contract=fee,
        bankroll=roll,
        kelly_fraction=kf,
        existing_fraction_total=existing,
        portfolio_cap=cap,
    )
    assert existing * roll + s.add_value <= max(cap * roll, existing * roll)
    assert s.quantity * s.cost <= s.add_value


@settings(max_examples=300, deadline=None)
@given(
    values=st.lists(st.integers(0, 1000).map(lambda n: D(n) / 1000), min_size=1, max_size=8),
    existing=st.integers(0, 50).map(lambda n: D(n) / 100),
    cap=st.integers(0, 50).map(lambda n: D(n) / 100),
)
def test_proportional_scaling_preserves_ordering_and_cap(values, existing, cap):
    new = {str(i): v for i, v in enumerate(values)}
    out = scale_to_cap(new, existing, cap)
    assert sum(out.values()) <= max(cap - existing, D(0)) or sum(out.values()) == sum(values)
    for a in new:
        assert out[a] <= new[a]
        for b in new:
            if new[a] <= new[b]:
                assert out[a] <= out[b]
