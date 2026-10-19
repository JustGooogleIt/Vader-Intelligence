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

from vader_intelligence.trading.executor import Executor
from vader_intelligence.trading.fees import KALSHI_2026_02_GENERAL as FEES
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
        report = executor.reconcile(now=at(12))
        assert coid in report.reconciled and report.clean
        lost = state(ledger, coid)
        assert lost.status == "rejected" and lost.reason == "not_found_at_venue"
        assert ledger.projection().portfolio.reserved_cash == 0
        third = place(executor, make_intent(t=13, rationale="third"), t=13)
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
        assert executor.reconcile(now=at(40)).clean
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
