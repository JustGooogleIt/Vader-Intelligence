"""Risk engine: fixed check order, all reasons collected, limits, bypass refusal."""

from dataclasses import replace

import pytest
from _trading_synthetic import (
    EVENT,
    OTHER,
    TICKER,
    D,
    approve,
    at,
    book,
    fill,
    gateway_event,
    make_intent,
    market,
    open_ledger,
    submit_requested,
)

from vader_intelligence.trading.fees import KALSHI_2026_02_GENERAL as FEES
from vader_intelligence.trading.instruments import Market, correlation_groups
from vader_intelligence.trading.ledger import LedgerConflict, build_order
from vader_intelligence.trading.orders import OrderEvent, client_order_id
from vader_intelligence.trading.portfolio import CashEvent
from vader_intelligence.trading.risk import (
    REASONS,
    SIMULATION_ASSUMPTIONS,
    SIMULATION_DEFAULTS,
    Approval,
    ForecastStatus,
    LimitsIncomplete,
    Rejection,
    RiskContext,
    RiskEngine,
    RiskLimits,
    simulation_defaults,
)

NOW = at(10)


def forecast(probability="0.55", t=0, ticker=TICKER, **kw):
    return ForecastStatus(ticker, D(probability), at(t), "synthetic-model", **kw)


def context(
    ledger,
    *,
    now=NOW,
    snap="default",
    fc="default",
    mkt="default",
    history=(),
    unknown=(),
    markets=None,
):
    limits, version = ledger.current_limits()
    snap = book(t=5) if snap == "default" else snap
    return RiskContext(
        environment=ledger.environment,
        now=now,
        projection=ledger.projection(),
        limits=limits,
        limits_version=version,
        kill_switch_engaged=ledger.kill_switch().engaged,
        fee_schedule=FEES,
        market=market() if mkt == "default" else mkt,
        snapshot=snap,
        forecast=forecast() if fc == "default" else fc,
        marks={TICKER: snap} if snap is not None else {},
        markets=markets or {},
        equity_history=history,
        unknown_state=unknown,
    )


def evaluate(ledger, intent=None, engine=None, **kw):
    engine = engine or RiskEngine(ledger.environment)
    return engine.evaluate(intent or make_intent(t=10), context(ledger, **kw))


def test_approval_sizes_with_fractional_kelly_and_records_ordered_checks(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        decision = evaluate(ledger)
        assert isinstance(decision, Approval)
        # c = 0.46 + 0.02 fee; f* = 0.07 / 0.52; f = 0.10 f*; 1000 x f / 0.48 -> 28
        assert decision.quantity == D(28) and decision.fee_bound_per_step == D("0.02")
        names = [c["check"] for c in decision.checks]
        assert names == [r for r in REASONS if r != "insufficient_contracts"]
        assert decision.sizing["method"] == "kelly" and decision.sizing["quantity"] == "28"
        assert decision.limits_version == SIMULATION_DEFAULTS.digest()


def test_all_failing_reasons_are_collected_in_spec_order(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        ledger.set_kill_switch(True, "drill", "operator", at())
        stale = book(t=-400)
        decision = evaluate(
            ledger, make_intent(price="0.995", t=10), snap=stale, fc=forecast(t=-4000)
        )
        assert isinstance(decision, Rejection)
        assert decision.reasons == (
            "kill_switch_engaged",
            "data_stale",
            "forecast_stale",
            "price_off_grid",
            "price_out_of_bounds",
            "price_deviation",
            "edge_below_minimum",
            "size_zero",
        )
        assert list(decision.reasons) == sorted(decision.reasons, key=REASONS.index)


def test_live_requires_every_limit_and_starts_with_kill_switch_engaged(tmp_path):
    partial = RiskLimits(max_order_contracts=D(10))
    with pytest.raises(LimitsIncomplete) as info:
        partial.require_complete()
    assert "kelly_fraction" in info.value.missing
    assert SIMULATION_DEFAULTS.require_complete() is SIMULATION_DEFAULTS
    with open_ledger(tmp_path / "none.sqlite3", "live", limits=None) as ledger:
        decision = evaluate(ledger)
        assert isinstance(decision, Rejection) and "limits_unset" in decision.reasons
    with open_ledger(tmp_path / "partial.sqlite3", "live", limits=partial) as ledger:
        for t in range(3):
            decision = evaluate(ledger, make_intent(t=10, rationale=f"try {t}"))
            assert "limits_unset" in decision.reasons
    history = ((at(-60), D(1000)),)
    with open_ledger(tmp_path / "full.sqlite3", "live") as ledger:
        assert evaluate(ledger, history=history).reasons == ("kill_switch_engaged",)
        ledger.set_kill_switch(False, "canary", "operator", at())
        assert isinstance(evaluate(ledger, history=history), Approval)
        blind = evaluate(ledger)  # live without equity history cannot evaluate loss stops
        assert blind.reasons == ("state_unknown",)
        assert "equity_history_missing" in blind.checks[2]["value"]
    assert any("SIMULATION ASSUMPTIONS" in a for a in SIMULATION_ASSUMPTIONS)
    assert simulation_defaults(D(2000)).max_event_exposure == D(100)


def test_freshness_forecast_and_market_gates(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        assert "data_stale" in evaluate(ledger, snap=book(t=20)).reasons  # from the future
        assert "data_stale" in evaluate(ledger, snap=None).reasons
        assert "forecast_unavailable" in evaluate(ledger, fc=None).reasons
        assert "forecast_unavailable" in evaluate(ledger, fc=forecast(compatible=False)).reasons
        assert "forecast_unavailable" in evaluate(ledger, fc=forecast("0.60")).reasons  # mismatch
        assert "market_unknown" in evaluate(ledger, mkt=None).reasons
        closed = market(close_time=at(5))
        assert "market_not_open" in evaluate(ledger, mkt=closed).reasons
        assert "price_deviation" in evaluate(ledger, make_intent(price="0.70", t=10)).reasons


def test_clip_to_limits_versus_reject(tmp_path):
    tight = replace(SIMULATION_DEFAULTS, max_order_contracts=D(10), max_event_exposure=D(3))
    with open_ledger(tmp_path / "l.sqlite3", limits=tight) as ledger:
        rejected = evaluate(ledger)
        assert rejected.reasons == ("order_contracts_limit", "event_exposure_limit")
        clipped = evaluate(ledger, engine=RiskEngine("paper", clip_to_limits=True))
        assert isinstance(clipped, Approval)
        assert clipped.quantity == D(6)  # $3 / (0.46 + 0.02 fee bound) per contract
        assert [c["check"] for c in clipped.sizing["clips"]] == [
            "order_contracts_limit",
            "event_exposure_limit",
        ]


def test_exposure_groups_buying_power_and_direction_conflict(tmp_path):
    with open_ledger(tmp_path / "small.sqlite3", cash="10") as ledger:
        decision = evaluate(ledger, make_intent(qty="20", t=10))
        assert "insufficient_buying_power" in decision.reasons  # the $50 buffer alone > $10
    a = Market("SYN-A-AAA", "SYN-A", teams=("AAA", "BBB"), game_date="2026-10-19")
    b = Market("SYN-B-AAA", "SYN-B", teams=("AAA", "CCC"), game_date="2026-10-19")
    c = Market("SYN-C-DDD", "SYN-C", teams=("DDD", "EEE"), game_date="2026-10-19")
    assert correlation_groups([a, b, c]) == {"SYN-A": "SYN-A", "SYN-B": "SYN-A", "SYN-C": "SYN-C"}
    with open_ledger(tmp_path / "group.sqlite3") as ledger:
        intent_a = make_intent(ticker=a.ticker, event=a.event_ticker, t=1)
        _, order = approve(ledger, intent_a, "10", t=1)
        ledger.append_event(submit_requested(order, 1))
        ledger.append_fill(fill(order, 10, t=1, fee="0.18"))
        snap_b = book(b.ticker, 5)
        decision = evaluate(
            ledger,
            make_intent(ticker=b.ticker, event=b.event_ticker, t=10),
            mkt=b,
            snap=snap_b,
            fc=forecast(ticker=b.ticker),
            markets={a.ticker: a, b.ticker: b},
        )
        assert decision.reasons == ("size_zero",)  # opposite direction in one cluster
        group_cap = replace(SIMULATION_DEFAULTS, max_group_exposure=D(5))
        ledger.record_limits(group_cap, "operator", at(2))
        same = evaluate(
            ledger,
            make_intent(ticker=a.ticker, event=a.event_ticker, t=10),
            mkt=a,
            snap=book(a.ticker, 5),
            fc=forecast(ticker=a.ticker),
            markets={a.ticker: a, b.ticker: b},
        )
        assert "group_exposure_limit" in same.reasons


def test_loss_stops_rate_and_open_order_limits(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        history = ((at(-23 * 3600), D(1000)), (at(-3600), D(1100)))
        decision = evaluate(ledger, history=history)  # daily loss 0; drawdown 100 < 150
        assert isinstance(decision, Approval)
        crash = ((at(-30), D(1200)),)
        stopped = evaluate(ledger, history=crash)  # daily loss 200 >= 50, drawdown 200 >= 150
        assert stopped.reasons == ("daily_loss_stop", "drawdown_stop")
        assert stopped.engage_kill_switch is True
        for i in range(10):
            intent = make_intent(t=5, rationale=f"o{i}")
            approve(ledger, intent, "1", t=5)
        decision = evaluate(ledger)
        assert "order_rate_limit" in decision.reasons
        later = evaluate(
            ledger, make_intent(t=100), now=at(100), snap=book(t=99), fc=forecast(t=99)
        )
        assert isinstance(later, Approval)  # the burst left the 60 s window
        limits = replace(SIMULATION_DEFAULTS, max_open_orders=10)
        ledger.record_limits(limits, "operator", at(1))
        decision = evaluate(
            ledger, make_intent(t=100), now=at(100), snap=book(t=99), fc=forecast(t=99)
        )
        assert "open_orders_limit" in decision.reasons


def test_unknown_state_blocks_buys_but_not_reduce_only_sells(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        _, held = approve(ledger, make_intent(t=1), "10", t=1)
        ledger.append_event(submit_requested(held, 1))
        ledger.append_fill(fill(held, 10, t=1, fee="0.18"))
        _, lost = approve(ledger, make_intent(t=2, rationale="lost"), "5", t=2)
        ledger.append_event(submit_requested(lost, 2))
        ledger.append_event(OrderEvent("u", lost.client_order_id, "submit_unknown", "local", at(2)))
        buy = evaluate(ledger)
        assert "state_unknown" in buy.reasons
        sell = evaluate(ledger, make_intent(action="sell", price="0.44", qty="4", t=10))
        assert isinstance(sell, Approval) and sell.quantity == D(4)
        ledger.set_kill_switch(True, "drill", "operator", at(3))
        assert isinstance(
            evaluate(
                ledger, make_intent(action="sell", price="0.44", qty="4", t=10, rationale="k")
            ),
            Approval,
        )
        over = evaluate(
            ledger, make_intent(action="sell", price="0.44", qty="40", t=10, rationale="over")
        )
        assert "insufficient_contracts" in over.reasons
        assert (
            "state_unknown"
            in evaluate(
                ledger, unknown=("balance_mismatch",), intent=make_intent(t=10, rationale="u2")
            ).reasons
        )


def test_reservation_conflict_for_opposite_buys_while_selling(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        _, held = approve(ledger, make_intent(t=1), "10", t=1)
        ledger.append_event(submit_requested(held, 1))
        ledger.append_fill(fill(held, 10, t=1))
        _, sell = approve(ledger, make_intent(action="sell", price="0.60", qty="10", t=2), "10")
        ledger.append_event(submit_requested(sell, 2))
        ledger.append_event(gateway_event(sell, "accepted", 2))
        decision = evaluate(ledger, make_intent(side="no", price="0.56", t=10), fc=forecast("0.30"))
        assert "reservation_conflict" in decision.reasons


def test_risk_bypass_attempts_are_refused(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        intent = make_intent(t=1)
        approval, order = approve(ledger, intent, "10", t=1)
        # An order that names no recorded approval.
        fake = replace(
            order, approval_id="f" * 64, client_order_id=client_order_id("paper", "f" * 64)
        )
        with pytest.raises(LedgerConflict, match="approval"):
            ledger.create_order(fake)
        # Reusing an approval for a second order.
        with pytest.raises(LedgerConflict):
            ledger.create_order(replace(order, client_order_id=order.client_order_id[:-1] + "2"))
        # Field mismatch between approval and order.
        other_intent = make_intent(t=2, rationale="second")
        ledger.record_intent(other_intent)
        approval2 = replace(
            approval, intent_id=other_intent.intent_id, approval_id="", created_at=at(2)
        )
        ledger.record_decision(approval2)
        good = build_order("paper", approval2, other_intent, at(2))
        for change in (
            {"quantity": D(11)},
            {"limit_price": D("0.45")},
            {"side": "no"},
            {"time_in_force": "gtc"},
            {"fee_bound_per_step": D(0)},
        ):
            with pytest.raises(LedgerConflict, match="differ"):
                ledger.create_order(replace(good, **change))
        ledger.create_order(good)
        # Approvals must cite recorded limits and pass every check; one decision per intent.
        third = make_intent(t=3, rationale="third")
        ledger.record_intent(third)
        with pytest.raises(LedgerConflict, match="unrecorded risk limits"):
            ledger.record_decision(
                replace(
                    approval, intent_id=third.intent_id, approval_id="", limits_version="0" * 64
                )
            )
        with pytest.raises(ValueError):
            replace(
                approval,
                checks=({"check": "x", "value": None, "limit": None, "passed": False},),
                approval_id="",
            )
        with pytest.raises(LedgerConflict):
            ledger.record_decision(Rejection(intent.intent_id, ("size_zero",), (), at(1)))
        unrecorded = make_intent(t=4, rationale="never recorded")
        with pytest.raises(LedgerConflict):
            ledger.record_decision(
                replace(approval, intent_id=unrecorded.intent_id, approval_id="")
            )
        assert ledger.verify() == []


def test_reduce_only_sells_need_no_forecast_or_cash_buffer(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        _, held = approve(ledger, make_intent(t=1), "10", t=1)
        ledger.append_event(submit_requested(held, 1))
        ledger.append_fill(fill(held, 10, t=1))
        ledger.append_cash_event(CashEvent("w", "withdrawal", D("990"), at(2)))
        sell = make_intent(action="sell", price="0.44", qty="10", t=10)
        decision = evaluate(ledger, sell, fc=None)
        assert isinstance(decision, Approval) and decision.quantity == D(10)
        assert "forecast_unavailable" in evaluate(ledger, fc=None).reasons  # buys still need it


def test_rejected_intent_cannot_redefine_a_tickers_event(tmp_path):
    limits = replace(SIMULATION_DEFAULTS, max_event_exposure=D(20), max_group_exposure=D(20))
    with open_ledger(tmp_path / "l.sqlite3", limits=limits) as ledger:
        poison = make_intent(t=1, event="SYN-BOGUS-EVENT", rationale="poison")
        assert "market_unknown" in evaluate(ledger, poison).reasons
        ledger.record_intent(poison)
        ledger.record_decision(evaluate(ledger, poison))
        _, held = approve(ledger, make_intent(t=2), "28", t=2)
        ledger.append_event(submit_requested(held, 2))
        ledger.append_fill(fill(held, 28, t=2, fee="0.49"))
        assert ledger.projection().event_of == {TICKER: EVENT}
        other = make_intent(ticker=OTHER, t=10)
        decision = evaluate(
            ledger, other, mkt=market(OTHER), snap=book(OTHER, 5), fc=forecast(ticker=OTHER)
        )
        assert isinstance(decision, Rejection) and "size_zero" in decision.reasons
