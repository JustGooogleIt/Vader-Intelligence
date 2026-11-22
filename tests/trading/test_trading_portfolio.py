"""Portfolio accounting: netting, settlement corrections, marks, invariants."""

import tempfile
from pathlib import Path

import pytest
from _trading_synthetic import (
    OTHER,
    TICKER,
    D,
    approve,
    at,
    book,
    fill,
    gateway_event,
    make_intent,
    open_ledger,
    submit_requested,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vader_intelligence.trading.fees import KALSHI_2026_02_GENERAL as FEES
from vader_intelligence.trading.ledger import LedgerConflict
from vader_intelligence.trading.orders import opposite
from vader_intelligence.trading.portfolio import PortfolioError, Settlement, replay


def placed(ledger, side, action, qty, price, t, fill_qty=None, fee="0", ticker=TICKER):
    intent = make_intent(ticker=ticker, side=side, action=action, price=price, t=t)
    _, order = approve(ledger, intent, qty, t=t)
    ledger.append_event(submit_requested(order, t))
    ledger.append_event(gateway_event(order, "accepted", t))
    if fill_qty:
        ledger.append_fill(fill(order, fill_qty, t=t, fee=fee))
    return order


def test_netting_sale_settlement_and_correction_by_hand(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        placed(ledger, "yes", "buy", "10", "0.40", 1, 10, fee="0.10")
        placed(ledger, "no", "buy", "4", "0.55", 2, 4, fee="0.07")
        port = ledger.projection().portfolio
        # 4 pairs netted for $4; YES cost 4.10 -> 2.46 (avg), NO cost 2.27 closed.
        assert port.cash == D("997.63") and port.realized == D("0.09")
        h = port.holding(TICKER)
        assert (h.yes_qty, h.yes_cost, h.no_qty, h.no_cost) == (D(6), D("2.46"), 0, 0)
        assert port.totals.netting_credits == D(4)
        placed(ledger, "yes", "sell", "3", "0.50", 3, 3, fee="0.05")
        port = ledger.projection().portfolio
        assert port.cash == D("999.08") and port.realized == D("0.31")
        assert port.holding(TICKER).yes_cost == D("1.23")
        ledger.append_settlement(Settlement("s1", TICKER, "yes", D(1), "synthetic", at(10)))
        port = ledger.projection().portfolio
        assert port.cash == D("1002.08") and port.realized == D("2.08") and not port.holdings
        with pytest.raises(LedgerConflict):
            ledger.append_settlement(Settlement("s2", TICKER, "no", D(0), "synthetic", at(11)))
        ledger.append_settlement(
            Settlement("s1-fix", TICKER, "no", D(0), "synthetic", at(12), supersedes_id="s1")
        )
        port = ledger.projection().portfolio
        assert port.cash == D("999.08") and port.realized == D("-0.92")
        assert [s.settlement_id for s in port.settlements.values()] == ["s1-fix"]
        with pytest.raises(LedgerConflict):  # only the current head can be superseded
            ledger.append_settlement(
                Settlement("s1-fix2", TICKER, "yes", D(1), "synthetic", at(13), supersedes_id="s1")
            )
        assert len(ledger.settlements()) == 2  # both recorded; nothing overwritten
        assert ledger.verify() == []


def test_marks_conservative_and_mid(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        placed(ledger, "yes", "buy", "10", "0.46", 1, 10)
        placed(ledger, "no", "buy", "5", "0.48", 2, 5, ticker=OTHER)
        port = ledger.projection().portfolio
        marks = {
            TICKER: book(TICKER, 3),
            OTHER: book(OTHER, 3, yes=(("0.50", 1),), no=(("0.47", 1),)),
        }
        value, missing = port.market_value(marks)
        assert value == D("4.40") + D("2.35") and missing == ()  # best bid of the held side
        mid, _ = port.market_value(marks, "mid")
        assert mid == D(10) * D("0.45") + D(5) * (1 - D("0.515"))
        assert port.market_value({}, "conservative") == (0, (TICKER, OTHER))
        assert port.equity(marks) == port.cash + value
        assert port.unrealized(marks) == value - port.remaining_cost


def test_replay_refuses_overselling_and_is_order_independent_for_cash():
    from vader_intelligence.trading.orders import Fill

    def f(i, side, action, qty, price):
        return Fill(
            f"f{i}",
            "vi-paper-x-1",
            TICKER,
            side,
            action,
            D(price),
            D(qty),
            D("0.01"),
            "taker",
            at(i),
            "simulated",
        )

    with pytest.raises(PortfolioError):
        replay([f(1, "yes", "sell", 1, "0.5")])
    buys = [
        f(1, "yes", "buy", 5, "0.40"),
        f(2, "no", "buy", 3, "0.55"),
        f(3, "yes", "buy", 2, "0.42"),
        f(4, "no", "buy", 6, "0.50"),
    ]
    a, b = replay(buys), replay(list(reversed(buys)))
    assert a.cash == b.cash and a.totals.netting_credits == b.totals.netting_credits == D(7)
    quantities = [{t: (h.yes_qty, h.no_qty) for t, h in p.holdings.items()} for p in (a, b)]
    assert quantities[0] == quantities[1] == {TICKER: (D(0), D(2))}
    assert a.cash + a.remaining_cost == a.realized
    assert b.cash + b.remaining_cost == b.realized


steps = st.lists(
    st.one_of(
        st.tuples(
            st.just("buy"),
            st.sampled_from(["yes", "no"]),
            st.integers(1, 30),
            st.integers(5, 95),
            st.integers(0, 100),
            st.sampled_from([TICKER, OTHER]),
        ),
        st.tuples(
            st.just("sell"),
            st.sampled_from(["yes", "no"]),
            st.integers(1, 30),
            st.integers(5, 95),
            st.integers(0, 100),
            st.sampled_from([TICKER, OTHER]),
        ),
        st.tuples(st.just("cancel"), st.integers(0, 50)),
        st.tuples(
            st.just("settle"), st.sampled_from(["yes", "no"]), st.sampled_from([TICKER, OTHER])
        ),
        st.tuples(
            st.just("correct"), st.sampled_from(["yes", "no"]), st.sampled_from([TICKER, OTHER])
        ),
    ),
    max_size=12,
)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(sequence=steps)
def test_accounting_invariants_under_random_sequences(sequence):
    with tempfile.TemporaryDirectory() as tmp:
        with open_ledger(Path(tmp) / "l.sqlite3") as ledger:
            deposits = D(1000)
            for t, step in enumerate(sequence, start=1):
                port = ledger.projection().portfolio
                kind = step[0]
                if kind in ("buy", "sell"):
                    _, side, qty, cents, pct, ticker = step
                    if ticker in port.settlements:
                        continue
                    if kind == "buy" and port.reserved_contracts.get((ticker, opposite(side))):
                        continue  # risk refuses this (reservation_conflict); see test_risk
                    if kind == "sell":
                        qty = min(qty, int(port.available(ticker, side)))
                        if qty <= 0:
                            continue
                    price = D(cents) / 100
                    order = placed(ledger, side, kind, str(qty), str(price), t, ticker=ticker)
                    filled = qty * pct // 100
                    if filled:
                        ledger.append_fill(
                            fill(order, filled, t=t, fee=FEES.fee(price, D(filled), "taker"))
                        )
                elif kind == "cancel":
                    active = ledger.projection().active_orders
                    if active:
                        target = active[step[1] % len(active)].order
                        ledger.append_event(gateway_event(target, "canceled", t))
                elif kind == "settle" and step[2] not in port.settlements:
                    ledger.append_settlement(
                        Settlement(
                            f"s{t}",
                            step[2],
                            step[1],
                            D(1 if step[1] == "yes" else 0),
                            "synthetic",
                            at(t),
                        )
                    )
                    for state in ledger.projection().active_orders:
                        if state.order.ticker == step[2]:
                            ledger.append_event(gateway_event(state.order, "expired", t))
                elif kind == "correct" and step[2] in port.settlements:
                    head = port.settlements[step[2]]
                    ledger.append_settlement(
                        Settlement(
                            f"c{t}",
                            step[2],
                            step[1],
                            D(1 if step[1] == "yes" else 0),
                            "synthetic",
                            at(t),
                            supersedes_id=head.settlement_id,
                        )
                    )
            assert ledger.verify() == []
            projection = ledger.projection()
            port = projection.portfolio
            assert port.cash + port.remaining_cost == deposits + port.realized
            assert port.reserved_cash >= 0 and port.cash >= port.reserved_cash
            for state in projection.orders.values():
                assert state.filled_quantity <= state.order.quantity
                if not state.active:
                    assert state.order.client_order_id not in port.order_reservations
