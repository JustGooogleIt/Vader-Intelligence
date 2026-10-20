"""Order state machine: idempotence, out-of-order delivery, races, reservations."""

from dataclasses import replace

import pytest
from _trading_synthetic import (
    TICKER,
    D,
    approve,
    at,
    fill,
    gateway_event,
    make_intent,
    open_ledger,
    submit_requested,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vader_intelligence.trading.ledger import LedgerConflict, build_order
from vader_intelligence.trading.orders import Order, OrderEvent, order_state
from vader_intelligence.trading.portfolio import Settlement
from vader_intelligence.trading.risk import Approval

ORDER = Order(
    "vi-paper-0123456789abcdef-1",
    "a" * 64,
    "i" * 64,
    TICKER,
    "yes",
    "buy",
    D(10),
    D("0.40"),
    "gtc",
    False,
    D(1),
    D("0.02"),
    at(),
)
COID = ORDER.client_order_id


def ev(kind, t, source="simulated", sequence=None, **payload):
    return OrderEvent(
        f"{kind}:{t}:{sequence}",
        COID,
        kind,
        source,
        at(t),
        sequence=sequence,
        exchange_ts=at(t),
        payload=payload,
    )


def fl(qty, t, fill_id=None):
    return fill(ORDER, qty, t=t, fill_id=fill_id or f"f{t}")


def test_lifecycle_and_terminal_states_never_reopen():
    req = OrderEvent("req", COID, "submit_requested", "local", at())
    assert order_state(ORDER).status == "new"
    assert order_state(ORDER, [req]).status == "pending_submit"
    assert order_state(ORDER, [req, ev("submitted", 1)]).status == "pending_submit"
    opened = order_state(ORDER, [req, ev("accepted", 2)])
    assert opened.status == "open" and opened.remaining == D(10)
    done = order_state(ORDER, [req, ev("accepted", 2)], [fl(4, 3), fl(6, 4)])
    assert done.status == "filled" and done.remaining == 0
    # Stale events after a terminal state do not reopen it.
    stale = order_state(ORDER, [req, ev("canceled", 5), ev("accepted", 9)])
    assert stale.status == "canceled"
    assert order_state(ORDER, [req, ev("accepted", 9)], [fl(10, 3)]).status == "filled"
    assert order_state(ORDER, [req, ev("expired", 3), ev("canceled", 4)]).status == "expired"


def test_cancel_fill_race():
    req = OrderEvent("req", COID, "submit_requested", "local", at())
    events = [
        req,
        ev("accepted", 1),
        OrderEvent("cr", COID, "cancel_requested", "local", at(2)),
        ev("canceled", 3, reason="user"),
    ]
    raced = order_state(ORDER, events, [fl(3, 4)])  # fill arrives after the cancel
    assert raced.status == "canceled" and raced.filled_quantity == D(3)
    assert raced.remaining == 0 and raced.cancel_requested
    completed = order_state(ORDER, events, [fl(3, 2), fl(7, 4)])
    assert completed.status == "filled"


def test_unknown_resolves_only_by_reconciliation():
    req = OrderEvent("req", COID, "submit_requested", "local", at())
    unknown = OrderEvent("u", COID, "submit_unknown", "local", at(1))
    assert order_state(ORDER, [req, unknown]).status == "unknown"
    assert order_state(ORDER, [req, unknown, ev("accepted", 2)]).status == "unknown"
    assert order_state(ORDER, [req, unknown, ev("reconciled", 3, status="open")]).status == "open"
    lost = order_state(ORDER, [req, unknown, ev("reconciled", 3, status="not_found")])
    assert lost.status == "rejected" and lost.reason == "not_found_at_venue"
    absent = ev("reconciled", 2, status="absent")  # one negative read resolves nothing
    still = order_state(ORDER, [req, unknown, absent])
    assert (still.status, still.venue_status, still.active) == ("unknown", None, True)
    assert order_state(ORDER, [req, unknown, absent, ev("accepted", 3)]).status == "unknown"
    found = order_state(ORDER, [req, unknown, absent, ev("reconciled", 3, status="open")])
    assert (found.status, found.venue_status) == ("open", "open")
    unresolved = order_state(ORDER, [req, absent])  # still without a submission outcome
    assert (unresolved.status, unresolved.submit_outcome) == ("pending_submit", False)
    assert order_state(ORDER, [req, unknown], [fl(10, 2)]).status == "filled"


def test_excess_fill_and_amendment_flag_inconsistency():
    assert order_state(ORDER, [], [fl(6, 1), fl(6, 2)]).inconsistent
    assert order_state(ORDER, [ev("amended", 1)]).inconsistent
    with pytest.raises(ValueError):
        order_state(ORDER, [], [fl(1, 1, "x"), fl(2, 1, "x")])  # conflicting duplicate


kinds = st.sampled_from(
    [
        "submitted",
        "accepted",
        "rejected",
        "canceled",
        "expired",
        "reconciled-open",
        "reconciled-canceled",
        "reconciled-not_found",
        "reconciled-absent",
        "cancel_requested",
        "submit_unknown",
    ]
)


@st.composite
def histories(draw):
    events = [OrderEvent("req", COID, "submit_requested", "local", at())]
    for i, kind in enumerate(draw(st.lists(kinds, max_size=6))):
        t = draw(st.integers(1, 50))
        seq = draw(st.one_of(st.none(), st.integers(0, 20)))
        if kind.startswith("reconciled"):
            events.append(
                OrderEvent(
                    f"rec{i}",
                    COID,
                    "reconciled",
                    "simulated",
                    at(t),
                    sequence=seq,
                    payload={"status": kind.split("-")[1]},
                )
            )
        elif kind in ("cancel_requested", "submit_unknown"):
            events.append(OrderEvent(f"{kind}{i}", COID, kind, "local", at(t)))
        else:
            events.append(
                OrderEvent(
                    f"{kind}{i}", COID, kind, "simulated", at(t), sequence=seq, exchange_ts=at(t)
                )
            )
    quantities = draw(st.lists(st.integers(1, 4), max_size=4))
    fills = [fl(q, i + 1, f"fill{i}") for i, q in enumerate(quantities)]
    return events, fills


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(history=histories(), data=st.data())
def test_duplicate_and_out_of_order_delivery_is_idempotent(history, data):
    events, fills = history
    expected = order_state(ORDER, events, fills)
    shuffled = data.draw(st.permutations(events + events[: data.draw(st.integers(0, 3))]))
    shuffled_fills = data.draw(st.permutations(fills + fills[:1]))
    assert order_state(ORDER, shuffled, shuffled_fills) == expected
    if expected.status in ("filled", "canceled", "expired", "rejected"):
        assert expected.remaining == 0


def submitted(ledger, intent=None, qty="10"):
    _, order = approve(ledger, intent or make_intent(), qty)
    ledger.append_event(submit_requested(order))
    return order


def reservation_of(ledger, order):
    return ledger.projection().portfolio.order_reservations.get(order.client_order_id)


@pytest.mark.parametrize(
    "path",
    [
        "filled",
        "canceled",
        "expired",
        "rejected",
        "not_found",
        "venue_canceled",
        "ioc_remainder",
        "never_submitted",
        "settled",
    ],
)
def test_reservations_released_on_every_terminal_path(tmp_path, path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        if path == "never_submitted":
            _, order = approve(ledger, make_intent(), "10")
            assert reservation_of(ledger, order) == (D("4.80"), D(0))  # 10*0.46 + 10*0.02
            ledger.append_event(
                OrderEvent(
                    "ns",
                    order.client_order_id,
                    "canceled",
                    "local",
                    at(1),
                    payload={"reason": "never_submitted"},
                )
            )
        else:
            order = submitted(ledger)
            assert reservation_of(ledger, order) == (D("4.80"), D(0))
            ledger.append_event(gateway_event(order, "accepted", 1))
            if path == "filled":
                ledger.append_fill(fill(order, 10, t=2, fee="0.17"))
            elif path in ("canceled", "expired", "rejected"):
                ledger.append_event(gateway_event(order, path, 2))
            elif path in ("not_found", "venue_canceled"):
                status = "not_found" if path == "not_found" else "canceled"
                ledger.append_event(gateway_event(order, "reconciled", 2, status=status))
            elif path == "ioc_remainder":
                ledger.append_fill(fill(order, 4, t=2, fee="0.08"))
                assert reservation_of(ledger, order) == (D("2.88"), D(0))
                ledger.append_event(gateway_event(order, "canceled", 2, reason="ioc_remainder"))
            elif path == "settled":
                ledger.append_settlement(Settlement("s", order.ticker, "no", D(0), "syn", at(3)))
                ledger.append_event(gateway_event(order, "expired", 3, reason="market_settled"))
        projection = ledger.projection()
        assert not projection.orders[order.client_order_id].active
        assert reservation_of(ledger, order) is None
        assert projection.portfolio.reserved_cash == 0
        assert ledger.verify() == []


def test_ledger_refuses_resubmission_and_invalid_fills(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        _, order = approve(ledger, make_intent(), "10")
        with pytest.raises(LedgerConflict):
            ledger.append_event(gateway_event(order, "accepted", 1))  # never submitted
        with pytest.raises(LedgerConflict):
            ledger.append_fill(fill(order, 1))
        ledger.append_event(submit_requested(order))
        again = OrderEvent("other-id", order.client_order_id, "submit_requested", "local", at(5))
        with pytest.raises(LedgerConflict, match="resubmission"):
            ledger.append_event(again)
        with pytest.raises(LedgerConflict, match="limit"):
            ledger.append_fill(fill(order, 1, price="0.47"))
        with pytest.raises(LedgerConflict, match="match"):
            ledger.append_fill(replace(fill(order, 1), side="no"))
        ledger.append_fill(fill(order, 8))
        with pytest.raises(LedgerConflict, match="exceed"):
            ledger.append_fill(fill(order, 3, t=1))
        sell = submitted(ledger, make_intent(action="sell", price="0.40", qty="9", t=2), "9")
        with pytest.raises(LedgerConflict, match="holding"):
            ledger.append_fill(fill(sell, 9))
        ledger.append_settlement(Settlement("s", order.ticker, "yes", D(1), "syn", at(9)))
        with pytest.raises(LedgerConflict, match="settled"):
            ledger.append_fill(fill(order, 1, t=9))
        assert ledger.verify() != []  # the open sell still reserves settled contracts
        ledger.append_event(gateway_event(sell, "expired", 9, reason="market_settled"))
        ledger.append_event(gateway_event(order, "expired", 9, reason="market_settled"))
        assert ledger.verify() == []


def test_order_must_match_its_approval(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        approval, order = approve(ledger, make_intent(), "10")
        assert isinstance(approval, Approval)
        assert (
            build_order("paper", approval, ledger.intent(approval.intent_id), order.created_at)
            == order
        )


def test_off_grid_fills_and_submission_after_local_cancel_are_refused(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3", cash="0.60") as ledger:
        _, order = approve(ledger, make_intent(price="0.05"), "10", fee_bound="0.01")
        ledger.append_event(submit_requested(order))
        with pytest.raises(LedgerConflict, match="grid"):
            ledger.append_fill(fill(order, "0.5", fee="0.01"))
        _, lost = approve(
            ledger, make_intent(price="0.05", t=1, rationale="b"), "1", t=1, fee_bound="0.01"
        )
        ledger.append_event(
            OrderEvent(
                "ns",
                lost.client_order_id,
                "canceled",
                "local",
                at(1),
                payload={"reason": "never_submitted"},
            )
        )
        with pytest.raises(LedgerConflict, match="first event"):
            ledger.append_event(submit_requested(lost, 2))
        assert ledger.verify() == []
