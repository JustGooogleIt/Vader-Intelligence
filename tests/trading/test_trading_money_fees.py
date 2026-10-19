"""Money helpers and hand-checked fee cases (synthetic numbers)."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vader_intelligence.trading.fees import (
    DEFAULT_FEE_SCHEDULE,
    KALSHI_2026_02_GENERAL,
    KALSHI_2026_02_MLB_ASSUMED,
    PROVISIONAL_SCHEDULES,
    FeeSchedule,
    fee_schedule_from_dict,
)
from vader_intelligence.trading.money import (
    ceil_to,
    dec,
    digest,
    floor_to,
    plain,
    text,
    utc,
)

D = Decimal
GENERAL = KALSHI_2026_02_GENERAL


def test_floats_and_bools_are_never_money():
    for bad in (0.5, True, float("nan")):
        with pytest.raises(TypeError):
            dec(bad)
    for bad in ("1e3", "NaN", "0x10", " 1", "1.", ""):
        with pytest.raises(ValueError):
            dec(bad)
    with pytest.raises(TypeError):
        plain({"x": 0.1})
    assert dec("0.5600") == D("0.56") and dec(3) == D(3)


def test_canonical_text_grids_and_timestamps():
    assert [text(D(x)) for x in ("0.5600", "100", "0E-8", "-0.0")] == ["0.56", "100", "0", "0"]
    assert floor_to(D("0.379"), D("0.01")) == D("0.37")
    assert ceil_to(D("0.371"), D("0.01")) == D("0.38")
    assert floor_to(D("-0.371"), D("0.01")) == D("-0.38")
    assert ceil_to(D("0.37"), D("0.01")) == D("0.37")
    assert floor_to(D("7.5"), D("2.5")) == D("7.5")
    with pytest.raises(ValueError):
        utc(datetime(2026, 1, 1))
    assert digest({"a": D("1.0")}) == digest({"a": D("1")})


def test_hand_checked_fee_cases():
    assert GENERAL.fee(D("0.50"), D(100), "taker") == D("1.75")
    assert GENERAL.fee(D("0.50"), D(1), "taker") == D("0.02")  # 0.0175 rounds up
    assert GENERAL.fee(D("0.10"), D(10), "taker") == D("0.07")  # 0.063 rounds up
    assert GENERAL.fee(D("0.50"), D(100), "maker") == D("0.44")  # 0.4375 rounds up
    assert GENERAL.fee(D("0.46"), D(28), "taker") == D("0.49")  # 0.486864 rounds up
    assert GENERAL.fee(D("0"), D(100), "taker") == 0 and GENERAL.fee(D("1"), D(5), "taker") == 0
    assert KALSHI_2026_02_MLB_ASSUMED.fee(D("0.50"), D(100), "taker") == D("0.88")  # 0.875
    sub_cent = replace(GENERAL, version="test-subcent", rounding_increment=D("0.0001"))
    assert sub_cent.fee(D("0.50"), D(1), "taker") == D("0.0175")
    assert GENERAL.fee_per_contract(D("0.50"), "taker") == D("0.02")
    # Fractional steps are priced per whole contract for edge/Kelly, not per tiny lot.
    assert GENERAL.fee_per_contract(D("0.50"), "taker", D("0.01")) == D("0.02")
    assert GENERAL.fee_per_contract(D("0.50"), "taker", D(10)) == D("0.018")
    assert GENERAL.fee_bound_per_step(D("0.30"), "buy") == D("0.02")  # ceil(0.07*0.21)
    assert GENERAL.fee_bound_per_step(D("0.70"), "buy") == D("0.02")  # worst price is 0.50
    assert GENERAL.fee_bound_per_step(D("0.05"), "buy", liquidity="maker") == D("0.01")


def test_schedules_are_versioned_data_flagged_unverified():
    assert DEFAULT_FEE_SCHEDULE is GENERAL  # higher-fee schedule is the paper default
    assert {s.version for s in PROVISIONAL_SCHEDULES} == {
        "kalshi-2026-02-general",
        "kalshi-2026-02-mlb-multiplier-0.5-assumed",
    }
    for schedule in PROVISIONAL_SCHEDULES:
        assert schedule.verified is False and schedule.assumptions
        assert schedule.taker_rate == D("0.07") and schedule.maker_rate == D("0.0175")
        assert fee_schedule_from_dict(schedule.to_dict()) == schedule
        manifest = schedule.manifest()
        assert manifest["verified"] is False and manifest["version"] == schedule.version
    assert KALSHI_2026_02_MLB_ASSUMED.fee_multiplier == D("0.5")
    assert any("ASSUMED" in a for a in KALSHI_2026_02_MLB_ASSUMED.assumptions)
    with pytest.raises(ValueError):
        FeeSchedule("x", None, None, False, (), D("0.07"), D("0"))  # unverified needs reasons
    with pytest.raises(TypeError):
        FeeSchedule("x", None, None, True, (), 0.07, D("0"))


prices = st.integers(1, 99).map(lambda c: D(c) / 100)


@settings(max_examples=200, deadline=None)
@given(
    limit=prices,
    lots=st.lists(st.integers(1, 40), min_size=1, max_size=6),
    offsets=st.lists(st.integers(0, 98), min_size=6, max_size=6),
    liquidity=st.lists(st.sampled_from(["maker", "taker"]), min_size=6, max_size=6),
    action=st.sampled_from(["buy", "sell"]),
)
def test_fee_bound_covers_any_partition_of_fills(limit, lots, offsets, liquidity, action):
    """Reservations stay sufficient however the order fills (per-fill rounding)."""
    bound = GENERAL.fee_bound_per_step(limit, action)
    total = D(0)
    for quantity, offset, liq in zip(lots, offsets, liquidity):
        cents = int(limit * 100)
        fill_cents = max(1, cents - offset) if action == "buy" else min(99, cents + offset)
        total += GENERAL.fee(D(fill_cents) / 100, D(quantity), liq)
    assert total <= sum(lots) * bound
