"""Executor: durable ordering around the gateway, unknown submissions, reconciliation.

The venue is the SYNTHETIC `FakeVenue` double; its state survives executor restarts.
"""

import pytest
from _trading_synthetic import (
    TICKER,
    D,
    FakeVenue,
    approve,
    at,
    book,
    make_intent,
    market,
    open_ledger,
)

from vader_intelligence.trading.executor import ABSENCE_GRACE_SECONDS, Executor
from vader_intelligence.trading.fees import KALSHI_2026_02_GENERAL as FEES
from vader_intelligence.trading.gateway import OrderStatus
from vader_intelligence.trading.ledger import EnvironmentMismatch, Ledger, LedgerError
from vader_intelligence.trading.risk import Approval, ForecastStatus, Rejection, RiskEngine


class Crash(BaseException):
    """Simulated process death (not an Exception: nothing may swallow it)."""


def forecast(t):
    return ForecastStatus(TICKER, D("0.55"), at(t), "synthetic-model")


def place(executor, intent=None, t=10, history=()):
    intent = intent or make_intent(t=t)
    return executor.place(
        intent,
        now=at(t),
        market=market(),
        snapshot=book(t=t - 5),
        forecast=forecast(t - 5),
        marks={TICKER: book(t=t - 5)},
        equity_history=history,
    )


def make(path, venue, environment="paper"):
    ledger = Ledger(path, environment)
    return ledger, Executor(ledger, venue, RiskEngine(environment), FEES)


def state(ledger, coid):
    return ledger.projection().orders[coid]


def test_happy_path_fills_reconcile_and_balances(tmp_path):
    open_ledger(tmp_path / "l.sqlite3").close()
    venue = FakeVenue()
    ledger, executor = make(tmp_path / "l.sqlite3", venue)
    with ledger:
        placement = place(executor)
        assert isinstance(placement.decision, Approval) and placement.submit.status == "accepted"
        coid = placement.order.client_order_id
        kinds = [e.kind for e in ledger.events(coid)]
        assert kinds == ["submit_requested", "accepted"]  # requested strictly before the call
        assert state(ledger, coid).status == "open"
        venue.execute(coid, 28, fee="0.49")
        report = executor.reconcile(now=at(20))
        assert report.clean, report.unresolved
        assert state(ledger, coid).status == "filled"
        assert ledger.projection().portfolio.cash == D("1000") - D(28) * D("0.46") - D("0.49")
        assert ledger.verify() == []
        again = place(executor)  # the same intent again: existing decision, no resubmission
        assert again.existing and venue.submit_calls == [coid]
        with ledger.transaction("outer"), pytest.raises(LedgerError, match="transaction"):
            place(executor, make_intent(t=11, rationale="nested"), t=11)


def test_unknown_submission_blocks_until_reconciled(tmp_path):
    open_ledger(tmp_path / "l.sqlite3").close()
    venue = FakeVenue()
    venue.fail_submit = "before"
    ledger, executor = make(tmp_path / "l.sqlite3", venue)
    with ledger:
        first = place(executor)
        coid = first.order.client_order_id
        assert first.submit.status == "unknown"
        assert state(ledger, coid).status == "unknown" and executor.needs_reconcile
        assert place(executor).existing and venue.submit_calls == [coid]
        venue.fail_submit = None
        venue.lookup_error = True
        blocked = place(executor, make_intent(t=11, rationale="second"), t=11)
        assert isinstance(blocked.decision, Rejection)
        assert "state_unknown" in blocked.decision.reasons
        assert venue.submit_calls == [coid]
        venue.lookup_error = False
        report = executor.reconcile(now=at(12))  # one negative read is never final
        assert coid in report.reconciled
        assert report.unresolved == (f"absence_unconfirmed:{coid}",)
        assert state(ledger, coid).status == "unknown"
        assert ledger.projection().portfolio.reserved_cash > 0
        report = executor.reconcile(now=at(11 + ABSENCE_GRACE_SECONDS))  # within the grace
        assert report.unresolved == (f"absence_unconfirmed:{coid}",)
        late = 12 + ABSENCE_GRACE_SECONDS
        report = executor.reconcile(now=at(late))  # second read after it, sweep clean
        assert coid in report.reconciled and report.clean
        lost = state(ledger, coid)
        assert lost.status == "rejected" and lost.reason == "not_found_at_venue"
        assert ledger.projection().portfolio.reserved_cash == 0
        third = place(executor, make_intent(t=late + 1, rationale="third"), t=late + 1)
        assert isinstance(third.decision, Approval) and third.submit.status == "accepted"
        assert place(executor).existing  # the lost order is never resubmitted
        assert venue.submit_calls == [coid, third.order.client_order_id]
        assert ledger.verify() == []


def test_timeout_after_venue_accepted_reconciles_to_open(tmp_path):
    open_ledger(tmp_path / "l.sqlite3").close()
    venue = FakeVenue()
    venue.fail_submit = "after"
    ledger, executor = make(tmp_path / "l.sqlite3", venue)
    with ledger:
        coid = place(executor).order.client_order_id
        assert state(ledger, coid).status == "unknown"
        venue.execute(coid, 10, fee="0.18")
        report = executor.reconcile(now=at(20))
        assert report.clean
        resolved = state(ledger, coid)
        assert resolved.status == "open" and resolved.filled_quantity == D(10)
        assert ledger.verify() == []


def test_cancel_fill_race_through_the_executor(tmp_path):
    open_ledger(tmp_path / "l.sqlite3").close()
    venue = FakeVenue()
    ledger, executor = make(tmp_path / "l.sqlite3", venue)
    with ledger:
        coid = place(executor, make_intent(t=10, tif="gtc")).order.client_order_id
        venue.execute(coid, 5, fee="0.09")  # fill happens at the venue before the cancel
        result = executor.cancel(coid, now=at(11))
        assert result.status == "canceled"
        assert state(ledger, coid).status == "canceled"
        assert executor.reconcile(now=at(12)).clean  # the late fill is still recorded
        final = state(ledger, coid)
        assert final.status == "canceled" and final.filled_quantity == D(5)
        assert ledger.projection().portfolio.reserved_cash == 0
        assert executor.cancel(coid, now=at(13)) is None
        assert ledger.verify() == []


def test_loss_stop_engages_persistent_kill_switch(tmp_path):
    path = tmp_path / "l.sqlite3"
    open_ledger(path).close()
    ledger, executor = make(path, FakeVenue())
    with ledger:
        stopped = place(executor, history=((at(-30), D(1200)),))
        assert "daily_loss_stop" in stopped.decision.reasons
        assert ledger.kill_switch().engaged and "daily_loss_stop" in ledger.kill_switch().reason
    with Ledger(path, "paper") as reopened:
        assert reopened.kill_switch().engaged
        executor = Executor(reopened, FakeVenue(), RiskEngine("paper"), FEES)
        assert "kill_switch_engaged" in place(executor, make_intent(t=12), t=12).decision.reasons


def test_reconcile_mismatch_and_never_submitted_orders(tmp_path):
    open_ledger(tmp_path / "l.sqlite3").close()
    venue = FakeVenue(cash="999")  # the venue disagrees about cash
    ledger, executor = make(tmp_path / "l.sqlite3", venue)
    with ledger:
        _, orphan = approve(ledger, make_intent(t=1, rationale="orphan"), "5", t=1)
        report = executor.reconcile(now=at(5))
        assert report.unresolved == ("balance_mismatch",)
        assert state(ledger, orphan.client_order_id).status == "canceled"
        assert "state_unknown" in place(executor).decision.reasons
        venue.cash = D(1000)
        assert executor.reconcile(now=at(15)).clean
        assert isinstance(place(executor, make_intent(t=16), t=16).decision, Approval)
    with pytest.raises(EnvironmentMismatch):
        with Ledger(tmp_path / "l.sqlite3", "paper") as again:
            Executor(again, FakeVenue("live"), RiskEngine("paper"), FEES)


def test_live_reconcile_records_observed_balance(tmp_path):
    open_ledger(tmp_path / "live.sqlite3", "live").close()
    venue = FakeVenue("live")
    ledger, executor = make(tmp_path / "live.sqlite3", venue, "live")
    with ledger:
        assert executor.reconcile(now=at(1)).clean
        assert [e.kind for e in ledger.cash_events()] == ["adjustment", "balance_observed"]
        assert "kill_switch_engaged" in place(executor).decision.reasons  # live default


def _run_place(path, venue, fault):
    ledger, executor = make(path, venue)
    ledger.fault = fault
    try:
        place(executor)
    finally:
        ledger.close()


def _finish(path, venue):
    """Restart: reconcile, re-propose the same intent, let the venue fill, reconcile."""
    ledger, executor = make(path, venue)
    with ledger:
        executor.reconcile(now=at(30))
        placement = place(executor, t=10)
        coid = placement.order.client_order_id if placement.order else None
        if coid and coid in venue.orders and venue.orders[coid]["status"] == "open":
            venue.execute(coid, 28, fee="0.49")
        # An order the venue never saw is final only on a second read after the grace.
        assert executor.reconcile(now=at(30 + ABSENCE_GRACE_SECONDS)).clean
        assert ledger.verify() == []
        projection = ledger.projection()
        return coid, projection.orders[coid].status, projection.portfolio.cash


def test_crash_between_every_durable_step_of_a_placement(tmp_path):
    clean_path = tmp_path / "clean.sqlite3"
    open_ledger(clean_path).close()
    points = []
    venue = FakeVenue()
    _run_place(clean_path, venue, points.append)
    clean = _finish(clean_path, venue)
    assert clean[1] == "filled"
    assert points == [
        "commit:decision",
        "committed:decision",
        "submit:before",
        "submit:after",
        "commit:event",
        "committed:event",
    ]
    for index, point in enumerate(points):
        path = tmp_path / f"crash-{index}.sqlite3"
        open_ledger(path).close()
        venue = FakeVenue()

        def fault(name, point=point):
            if name == point:
                raise Crash(name)

        with pytest.raises(Crash):
            _run_place(path, venue, fault)
        coid, status, cash = _finish(path, venue)
        assert coid == clean[0]
        assert venue.submit_calls.count(coid) <= 1  # never blindly resubmitted
        if point in ("committed:decision", "submit:before"):
            # The venue never saw the order: reconciliation proves it, nothing is retried.
            assert status == "rejected" and cash == D(1000)
        else:
            assert (status, cash) == clean[1:]


class HiddenVenue(FakeVenue):
    """`lookup` misses the orders in `hidden` (a lagging read); `open_orders` misses those
    in `unlisted` and raises while `open_error` is set."""

    def __init__(self):
        super().__init__()
        self.hidden, self.unlisted, self.open_error = set(), set(), False

    def lookup(self, coid):
        return None if coid in self.hidden else super().lookup(coid)

    def open_orders(self):
        if self.open_error:
            raise ConnectionError("synthetic open-orders failure")
        return [
            OrderStatus(c, "open", e["filled"])
            for c, e in sorted(self.orders.items())
            if e["status"] == "open" and c not in self.unlisted
        ]


def test_absence_needs_a_second_read_and_a_clean_open_orders_sweep(tmp_path):
    open_ledger(tmp_path / "l.sqlite3").close()
    venue = HiddenVenue()
    venue.fail_submit = "after"  # the venue holds the order; its answer was lost
    ledger, executor = make(tmp_path / "l.sqlite3", venue)
    with ledger:
        coid = place(executor).order.client_order_id
        venue.hidden.add(coid)
        unconfirmed = f"absence_unconfirmed:{coid}"
        assert executor.reconcile(now=at(15)).unresolved == (unconfirmed,)  # sweep lists it
        assert ledger.events(coid)[-1].kind == "submit_unknown"  # no negative read yet
        venue.unlisted.add(coid)
        assert executor.reconcile(now=at(20)).unresolved == (unconfirmed,)  # absent once
        venue.unlisted.clear()
        late = 20 + ABSENCE_GRACE_SECONDS
        assert executor.reconcile(now=at(late)).unresolved == (unconfirmed,)  # sweep lists it
        venue.open_error = True
        report = executor.reconcile(now=at(late + 1))  # no sweep: absence stays unconfirmed
        assert set(report.unresolved) == {unconfirmed, "open_orders_unavailable"}
        assert state(ledger, coid).status == "unknown"
        assert [e.payload["status"] for e in ledger.events(coid) if e.kind == "reconciled"] == [
            "absent"  # the first negative read is recorded once
        ]
        venue.open_error = False
        venue.hidden.clear()
        assert executor.reconcile(now=at(late + 2)).clean  # found after all
        assert state(ledger, coid).status == "open"
        assert ledger.verify() == []


def test_an_order_ended_only_by_absence_is_flagged_and_cancelable(tmp_path):
    open_ledger(tmp_path / "l.sqlite3").close()
    venue = HiddenVenue()
    venue.fail_submit = "after"
    ledger, executor = make(tmp_path / "l.sqlite3", venue)
    with ledger:
        coid = place(executor).order.client_order_id
        venue.hidden.add(coid)
        venue.unlisted.add(coid)  # a lag on every read
        executor.reconcile(now=at(20))
        late = 20 + ABSENCE_GRACE_SECONDS
        assert executor.reconcile(now=at(late)).clean
        assert state(ledger, coid).status == "rejected"
        venue.hidden.clear()
        venue.unlisted.clear()  # it surfaces, resting
        report = executor.reconcile(now=at(late + 1))
        assert f"venue_open_untracked:{coid}" in report.unresolved
        blocked = place(executor, make_intent(t=late + 2, rationale="next"), t=late + 2)
        assert "state_unknown" in blocked.decision.reasons
        venue.execute(coid, 4, fee="0.07")
        result = executor.cancel(coid, now=at(late + 3))
        assert result is not None and result.status == "canceled"
        assert venue.orders[coid]["status"] == "canceled"
        final = state(ledger, coid)
        assert final.status == "rejected" and final.remaining == 0  # never reopened
        assert executor.cancel(coid, now=at(late + 4)) is None
        assert executor.reconcile(now=at(late + 5)).clean  # its fill is recorded
        assert state(ledger, coid).filled_quantity == D(4)
        assert ledger.verify() == []
