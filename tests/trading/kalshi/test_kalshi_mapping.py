"""Mapping: all four side/action combinations, the price grid, fractional counts and
the round trip of exchange order and fill objects. SYNTHETIC values only."""

from decimal import Decimal

import pytest
from _trading_synthetic import TICKER, D, at
from hypothesis import given, settings
from hypothesis import strategies as st

from vader_intelligence.trading.kalshi.mapping import (
    MappingError,
    book_side,
    fill_from_exchange,
    from_ms,
    mismatches,
    order_request,
    order_status,
    price_ranges,
    rfc3339,
    side_action,
    side_price,
    venue_order,
    yes_price,
)
from vader_intelligence.trading.orders import Order

from ._kalshi_double import LINEAR_CENT, fp

LINEAR = price_ranges(LINEAR_CENT)
TAPERED = price_ranges(
    [
        {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
        {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
        {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
    ]
)


def order(side="yes", action="buy", price="0.46", qty="10", tif="gtc", post_only=False):
    return Order(
        client_order_id="vi-live-0123456789abcdef-1",
        approval_id="a" * 64,
        intent_id="b" * 64,
        ticker=TICKER,
        side=side,
        action=action,
        quantity=D(qty),
        limit_price=D(price),
        time_in_force=tif,
        post_only=post_only,
        qty_step=D("0.01"),
        fee_bound_per_step=D("0.01"),
        created_at=at(0),
    )


@pytest.mark.parametrize(
    "side, action, price, book, wire_price",
    [
        ("yes", "buy", "0.46", "bid", "0.4600"),
        ("yes", "sell", "0.46", "ask", "0.4600"),
        ("no", "buy", "0.40", "ask", "0.6000"),  # facts §2.1: buy NO at 0.40 = ask at 0.60
        ("no", "sell", "0.40", "bid", "0.6000"),
    ],
)
def test_four_side_action_combinations(side, action, price, book, wire_price):
    body = order_request(order(side, action, price), LINEAR, 3).body()
    assert body == {
        "ticker": TICKER,
        "side": book,
        "price": wire_price,
        "count": "10.00",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": "vi-live-0123456789abcdef-1",
        "post_only": False,
        "reduce_only": False,
        "exchange_index": 3,
    }
    assert side_action(book, side) == (side, action)
    assert side_price(side, D(wire_price)) == D(price)


@settings(max_examples=200, deadline=None)
@given(
    side=st.sampled_from(["yes", "no"]),
    action=st.sampled_from(["buy", "sell"]),
    ticks=st.integers(min_value=1, max_value=9999),
)
def test_round_trip_property(side, action, ticks):
    price = Decimal(ticks) / 10000
    assert side_action(book_side(side, action), side) == (side, action)
    assert side_price(side, yes_price(side, price)) == price
    assert ZERO_ONE(yes_price(side, price))


def ZERO_ONE(value):
    return Decimal(0) < value < Decimal(1)


def test_price_grid_is_the_source_of_truth():
    assert order_request(order(price="0.95"), LINEAR).price == D("0.95")
    for bad in ("0.955", "0.0050"):
        with pytest.raises(MappingError) as info:
            order_request(order(price=bad), LINEAR)
        assert info.value.code == "price_off_grid"
    assert order_request(order(price="0.955"), TAPERED).body()["price"] == "0.9550"
    assert order_request(order(price="0.005"), TAPERED).body()["price"] == "0.0050"
    with pytest.raises(MappingError):
        order_request(order(price="0.155"), TAPERED)  # centre band is whole cents
    with pytest.raises(MappingError):
        order_request(order(price="0.12345"), TAPERED)  # beyond 4 decimals
    with pytest.raises(MappingError):  # NO at 0.999 is YES at 0.001: on the edge grid only
        order_request(order(side="no", price="0.999"), LINEAR)
    assert order_request(order(side="no", price="0.999"), TAPERED).body()["price"] == "0.0010"


def test_fractional_counts_on_hundredths():
    assert order_request(order(qty="2.5"), LINEAR).body()["count"] == "2.50"
    assert order_request(order(qty="0.01"), LINEAR).body()["count"] == "0.01"
    with pytest.raises(ValueError):  # the Order itself refuses an off-step quantity
        order(qty="0.005")
    coarse = Order(**{**order().to_dict(), "qty_step": "0.001", "quantity": "1.005"})
    with pytest.raises(MappingError) as info:
        order_request(coarse, LINEAR)
    assert info.value.code == "count_off_grid"


def test_time_in_force_reduce_only_and_post_only():
    ioc_sell = order_request(order(action="sell", tif="ioc"), LINEAR).body()
    assert ioc_sell["time_in_force"] == "immediate_or_cancel" and ioc_sell["reduce_only"] is True
    gtc_sell = order_request(order(action="sell", tif="gtc"), LINEAR).body()
    assert gtc_sell["reduce_only"] is False  # reduce_only requires IOC (facts §2.1)
    ioc_buy = order_request(order(tif="ioc"), LINEAR).body()
    assert ioc_buy["reduce_only"] is False
    maker = order_request(order(post_only=True), LINEAR).body()
    assert maker["post_only"] is True
    assert "exchange_index" not in order_request(order(), LINEAR).body()


def exchange_order(o, status="resting", filled="0", **overrides):
    request = order_request(o, LINEAR)
    data = {
        "order_id": "0000000a-0000-4000-8000-00000000000a",
        "client_order_id": o.client_order_id,
        "ticker": o.ticker,
        "outcome_side": "no",
        "book_side": request.side,
        "type": "limit",
        "status": status,
        "yes_price_dollars": fp(request.price, 6),
        "no_price_dollars": fp(1 - request.price, 6),
        "initial_count_fp": fp(o.quantity, 2),
        "fill_count_fp": fp(filled, 2),
        "remaining_count_fp": fp(o.quantity - D(filled) if status == "resting" else 0, 2),
        "taker_fees_dollars": "0.010000",
        "maker_fees_dollars": "0.000000",
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "status, filled, mapped", [("resting", "3", "open"), ("canceled", "3", "canceled")]
)
def test_exchange_order_round_trip(status, filled, mapped):
    for side, action in (("yes", "buy"), ("no", "buy"), ("yes", "sell"), ("no", "sell")):
        o = order(side, action, "0.37")
        venue = venue_order(exchange_order(o, status, filled))
        assert mismatches(o, venue) == []
        result = order_status(venue)
        assert (result.status, result.filled_quantity) == (mapped, D(filled))
        assert result.client_order_id == o.client_order_id
        assert venue.fees == D("0.01")
    executed = venue_order(exchange_order(order(), "executed", "10"))
    assert order_status(executed).status == "filled"


def test_exchange_order_mismatches_and_invalid_objects():
    o = order("no", "buy", "0.40")
    wrong = venue_order(exchange_order(o, yes_price_dollars="0.4000"))
    assert mismatches(o, wrong) == ["yes_price"]
    other = venue_order(exchange_order(o, book_side="bid", ticker=TICKER + "X"))
    assert mismatches(o, other) == ["ticker", "book_side"]
    for bad in (
        {"status": "pending"},
        {"status": "executed", "fill_count_fp": "3.00"},
        {"fill_count_fp": "11.00"},
        {"yes_price_dollars": 0.6},
        {"order_id": "../../portfolio/balance"},
        {"book_side": "buy"},
    ):
        with pytest.raises(MappingError):
            venue_order(exchange_order(o, **bad))


def exchange_fill(o, count="2.00", yes=None, taker=True, fee="0.020000", **overrides):
    request = order_request(o, LINEAR)
    y = D(yes) if yes is not None else request.price
    data = {
        "fill_id": "f0000001-synthetic",
        "trade_id": "f0000001-synthetic",
        "order_id": "0000000a-0000-4000-8000-00000000000a",
        "ticker": o.ticker,
        "book_side": request.side,
        "outcome_side": "yes",
        "count_fp": count,
        "yes_price_dollars": fp(y, 6),
        "no_price_dollars": fp(1 - y, 6),
        "is_taker": taker,
        "created_time": "2026-10-19T22:00:05.123456789Z",
        "fee_cost": fee,
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "side, action, yes, expected",
    [
        ("yes", "buy", "0.45", "0.45"),
        ("yes", "sell", "0.47", "0.47"),
        ("no", "buy", "0.61", "0.39"),
        ("no", "sell", "0.59", "0.41"),
    ],
)
def test_exchange_fill_to_fill(side, action, yes, expected):
    o = order(side, action, "0.46" if side == "yes" else "0.40")
    fill = fill_from_exchange(exchange_fill(o, "2.50", yes, taker=False), o)
    assert (fill.side, fill.action, fill.price, fill.quantity) == (
        side,
        action,
        D(expected),
        D("2.5"),
    )
    assert fill.fee == D("0.02") and fill.liquidity == "maker" and fill.source == "exchange"
    assert fill.fill_id == "kalshi:f0000001-synthetic"
    assert fill.exchange_ts == at(5).replace(microsecond=123456)
    assert fill_from_exchange(exchange_fill(o), o).liquidity == "taker"


def test_exchange_fill_inconsistencies_fail_closed():
    o = order("no", "buy", "0.40")
    for bad in (
        {"book_side": "bid"},
        {"ticker": "SYN-OTHER-X"},
        {"no_price_dollars": "0.300000"},
        {"fee_cost": "-0.01"},
        {"is_taker": 1},
        {"count_fp": "0.005"},
        {"created_time": "yesterday"},
        {"fill_id": "bad id with spaces"},
    ):
        with pytest.raises(MappingError):
            fill_from_exchange(exchange_fill(o, **bad), o)


def test_time_parsing():
    assert rfc3339("2026-10-19T18:00:00-04:00") == at(0)
    assert rfc3339("2026-10-19T22:00:00Z") == at(0)
    assert from_ms(1760911200000).isoformat() == "2025-10-19T22:00:00+00:00"
    for bad in ("2026-10-19 22:00:00", "2026-13-01T00:00:00Z", 5):
        with pytest.raises(MappingError):
            rfc3339(bad)
