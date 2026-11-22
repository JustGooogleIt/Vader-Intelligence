"""Ledger guarantees: environment separation, append-only history, idempotency."""

import sqlite3

import pytest
from _trading_synthetic import (
    D,
    approve,
    at,
    fill,
    gateway_event,
    make_intent,
    open_ledger,
    submit_requested,
)

from vader_intelligence.storage import Store
from vader_intelligence.trading import ledger as ledger_module
from vader_intelligence.trading.ledger import (
    EnvironmentMismatch,
    Ledger,
    LedgerConflict,
    LedgerError,
    LedgerLocked,
)
from vader_intelligence.trading.portfolio import CashEvent


def submitted_order(ledger, qty="10"):
    _, order = approve(ledger, make_intent(), qty)
    ledger.append_event(submit_requested(order))
    return order


def test_paper_ledger_refuses_exchange_records(tmp_path):
    with open_ledger(tmp_path / "paper.sqlite3") as ledger:
        order = submitted_order(ledger)
        with pytest.raises(EnvironmentMismatch):
            ledger.append_fill(fill(order, 1, src="exchange"))
        with pytest.raises(EnvironmentMismatch):
            ledger.append_event(gateway_event(order, "accepted", src="exchange"))
        with pytest.raises(EnvironmentMismatch):
            ledger.append_cash_event(CashEvent("b", "balance_observed", D(5), at()))
        # The SQL triggers refuse it too, independent of the Python checks.
        with pytest.raises(sqlite3.IntegrityError, match="environment guard"):
            ledger.db.execute(
                "INSERT INTO fills(fill_id,client_order_id,ticker,source,digest,data_json) "
                "VALUES ('x',?,?,'exchange','d','{}')",
                (order.client_order_id, order.ticker),
            )
        with pytest.raises(sqlite3.IntegrityError, match="environment guard"):
            ledger.db.execute(
                "INSERT INTO order_events(event_id,client_order_id,kind,source,digest,data_json) "
                "VALUES ('e',?,'accepted','exchange','d','{}')",
                (order.client_order_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="environment guard"):
            ledger.db.execute(
                "INSERT INTO cash_events(event_id,kind,digest,data_json) "
                "VALUES ('c','balance_observed','d','{}')"
            )
        ledger.append_fill(fill(order, 1))
        assert ledger.verify() == []


def test_live_ledger_refuses_simulated_records(tmp_path):
    with open_ledger(tmp_path / "live.sqlite3", "live") as ledger:
        order = submitted_order(ledger)
        with pytest.raises(EnvironmentMismatch):
            ledger.append_fill(fill(order, 1, src="simulated"))
        with pytest.raises(EnvironmentMismatch):
            ledger.append_event(gateway_event(order, "accepted", src="simulated"))
        with pytest.raises(EnvironmentMismatch):
            ledger.append_cash_event(CashEvent("d", "deposit", D(5), at()))
        with pytest.raises(sqlite3.IntegrityError, match="environment guard"):
            ledger.db.execute(
                "INSERT INTO fills(fill_id,client_order_id,ticker,source,digest,data_json) "
                "VALUES ('x',?,?,'simulated','d','{}')",
                (order.client_order_id, order.ticker),
            )
        ledger.append_fill(fill(order, 1, src="exchange"))
        assert order.client_order_id.startswith("vi-live-")
        assert ledger.verify() == []


def test_environment_is_immutable_and_checked_on_open(tmp_path):
    path = tmp_path / "paper.sqlite3"
    with open_ledger(path) as ledger:
        for sql in (
            "UPDATE ledger_meta SET value='live' WHERE key='environment'",
            "DELETE FROM ledger_meta WHERE key='environment'",
            "INSERT OR REPLACE INTO ledger_meta VALUES ('environment','live')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                ledger.db.execute(sql)
    with pytest.raises(EnvironmentMismatch):
        Ledger(path, "live")
    with pytest.raises(ValueError):
        Ledger(tmp_path / "x.sqlite3", "staging")


def test_event_tables_are_append_only(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        order = submitted_order(ledger)
        ledger.append_fill(fill(order, 2))
        ledger.set_kill_switch(True, "test", "operator", at())
        for table in (
            "intents",
            "approvals",
            "orders",
            "order_events",
            "fills",
            "cash_events",
            "kill_switch",
            "risk_limit_versions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                ledger.db.execute(f"UPDATE {table} SET digest='x'")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                ledger.db.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger.db.execute("INSERT OR REPLACE INTO orders SELECT * FROM orders")
        assert ledger.verify() == []


class Abort(Exception):
    pass


def records(ledger):
    """Every cached table read straight from the records (no parsed-row reuse)."""
    readers = {
        "intents": ledger.intents,
        "approvals": ledger.approvals,
        "rejections": ledger.rejections,
        "orders": ledger.orders,
        "order_events": ledger.events,
        "fills": ledger.fills,
        "settlements": ledger.settlements,
        "cash_events": ledger.cash_events,
    }
    assert set(readers) == ledger_module._ROW_CACHED
    cached = {table: read() for table, read in readers.items()}
    saved, ledger._committed = ledger._committed, {}
    try:
        fresh = {table: read() for table, read in readers.items()}
    finally:
        ledger._committed = saved
    return cached, fresh


def test_cached_table_reads_equal_the_records_across_rollbacks_and_readers(tmp_path):
    tables = {table for table, _ in ledger_module._APPEND_ONLY}
    assert ledger_module._ROW_CACHED <= tables  # only append-only tables are cached
    path = tmp_path / "c.sqlite3"
    with open_ledger(path) as ledger:
        order = submitted_order(ledger)
        ledger.append_fill(fill(order, 2, t=1))
        cached, fresh = records(ledger)
        assert cached == fresh and len(cached["fills"]) == 1
        # Rows written inside a transaction are visible there but never kept: after the
        # rollback the next fill reuses the rowid (seq) and must replace the phantom.
        with pytest.raises(Abort):
            with ledger.transaction("aborted"):
                ledger.append_fill(fill(order, 3, t=2))
                assert [f.quantity for f in ledger.fills()] == [D(2), D(3)]
                raise Abort
        assert [f.quantity for f in ledger.fills()] == [D(2)]
        ledger.append_fill(fill(order, 4, t=3))
        cached, fresh = records(ledger)
        assert cached == fresh and [f.quantity for f in cached["fills"]] == [D(2), D(4)]
        with Ledger(path, "paper", readonly=True) as reader:
            assert reader.fills() == ledger.fills()
            ledger.append_fill(fill(order, 1, t=4))
            ledger.append_cash_event(CashEvent("dep-c", "deposit", D(5), at()))
            assert reader.fills() == ledger.fills() and len(reader.fills()) == 3
            assert reader.cash_events() == ledger.cash_events()
        assert ledger.projection().orders[order.client_order_id].filled_quantity == D(7)
        # verify() recomputes from the records on disk, not from rows parsed earlier.
        ledger.db.execute("DROP TRIGGER fills_no_update")
        ledger.db.execute("UPDATE fills SET data_json=replace(data_json, '\"4\"', '\"5\"')")
        assert any("digest mismatch" in f for f in ledger.verify())
        assert [f.quantity for f in ledger.fills()] == [D(2), D(5), D(1)]


def test_idempotent_appends_and_conflicts(tmp_path):
    with open_ledger(tmp_path / "m.sqlite3") as ledger:
        intent = make_intent()
        assert ledger.record_intent(intent) is True
        assert ledger.record_intent(intent) is False
        assert ledger.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
        event = CashEvent("dep-2", "deposit", D(5), at())
        assert ledger.append_cash_event(event) is True
        assert ledger.append_cash_event(event) is False
        with pytest.raises(LedgerConflict):
            ledger.append_cash_event(CashEvent("dep-2", "deposit", D(6), at()))
        order = submitted_order(ledger)
        f = fill(order, 3)
        assert ledger.append_fill(f) is True and ledger.append_fill(f) is False
        with pytest.raises(LedgerConflict):
            ledger.append_fill(fill(order, 4, fill_id=f.fill_id))
        assert ledger.projection().orders[order.client_order_id].filled_quantity == D(3)


def test_kill_switch_defaults_and_persists_across_reopen(tmp_path):
    paper, live = tmp_path / "p.sqlite3", tmp_path / "l.sqlite3"
    with open_ledger(paper) as ledger:
        state = ledger.kill_switch()
        assert state.engaged is False and state.default
        ledger.set_kill_switch(True, "drill", "operator", at())
    with Ledger(paper, "paper") as ledger:
        state = ledger.kill_switch()
        assert state.engaged is True and not state.default and state.reason == "drill"
    with open_ledger(live, "live") as ledger:
        assert ledger.kill_switch().engaged is True  # live starts engaged
        ledger.set_kill_switch(False, "canary approved", "operator", at())
        # Re-engaging with an identical (reason, actor, at) after a disengage still counts.
        ledger.set_kill_switch(True, "halt", "operator", at(1))
        ledger.set_kill_switch(False, "resume", "operator", at(2))
        ledger.set_kill_switch(True, "halt", "operator", at(1))
        ledger.set_kill_switch(True, "halt", "operator", at(1))  # repeat: no new row
        assert ledger.db.execute("SELECT COUNT(*) FROM kill_switch").fetchone()[0] == 4
        ledger.set_kill_switch(False, "canary approved", "operator", at())
    with Ledger(live, "live") as ledger:
        assert ledger.kill_switch().engaged is False


def test_writer_lock_readonly_and_non_ledger_refusal(tmp_path):
    path = tmp_path / "l.sqlite3"
    with open_ledger(path) as ledger:
        with pytest.raises(LedgerLocked):
            Ledger(path, "paper")
        with Ledger(path, "paper", readonly=True) as reader:
            assert reader.verify() == []
            with pytest.raises(LedgerError):
                reader.set_kill_switch(True, "x", "y", at())
            assert reader.projection().portfolio.cash == D(1000)
            ledger.append_cash_event(CashEvent("dep-late", "deposit", D(5), at()))
            assert reader.projection().portfolio.cash == D(1005)  # no stale cache
        assert ledger.kill_switch().engaged is False
    archive = tmp_path / "archive.sqlite3"
    Store(archive, min_free_bytes=1).close()
    with pytest.raises(LedgerError):
        Ledger(archive, "paper")
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "missing.sqlite3", "paper", create=False)


def test_backup_verifies_and_tampering_is_detected(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        order = submitted_order(ledger)
        ledger.append_fill(fill(order, 2, fee="0.02"))
        assert ledger.backup(tmp_path / "copy.sqlite3") == []
        with pytest.raises(LedgerError):
            ledger.backup(tmp_path / "copy.sqlite3")
        ledger.db.execute("DROP TRIGGER fills_no_update")
        ledger.db.execute("UPDATE fills SET data_json=replace(data_json, '\"2\"', '\"1\"')")
        failures = ledger.verify()
        assert any("digest mismatch" in f for f in failures)
        assert any("missing guard triggers: fills_no_update" in f for f in failures)
        with pytest.raises(ValueError):
            ledger.set_checkpoint("run:identity", {}, at())  # insert-once keys stay immutable
        with pytest.raises(ValueError):
            ledger.mark_processed("checkpoint:x", {}, at())


def test_decisions_are_exclusive_per_intent(tmp_path):
    with open_ledger(tmp_path / "l.sqlite3") as ledger:
        intent = make_intent()
        approve(ledger, intent, "5")
        with pytest.raises(sqlite3.IntegrityError, match="intent already decided"):
            ledger.db.execute(
                "INSERT INTO rejections(rejection_id,intent_id,created_at,digest,data_json) "
                "VALUES ('r',?,'t','d','{}')",
                (intent.intent_id,),
            )


def test_a_live_approval_must_cite_complete_recorded_limits(tmp_path):
    from vader_intelligence.trading.risk import RiskLimits

    incomplete = RiskLimits(max_order_contracts=D(1))
    with open_ledger(tmp_path / "live.sqlite3", "live", limits=incomplete) as ledger:
        with pytest.raises(LedgerConflict, match="incomplete limits"):
            approve(ledger, make_intent(), "1")
    with open_ledger(tmp_path / "paper.sqlite3", "paper", limits=incomplete) as ledger:
        approve(ledger, make_intent(), "1")  # paper ledgers may simulate partial limits


def test_a_fill_fee_above_the_order_bound_is_refused(tmp_path):
    with open_ledger(tmp_path / "f.sqlite3") as ledger:
        order = submitted_order(ledger, qty="10")
        with pytest.raises(LedgerConflict, match="fee bound"):
            ledger.append_fill(fill(order, 2, fee="0.05"))  # bound: 2 lots x 0.02
        ledger.append_fill(fill(order, 2, fee="0.04"))
        assert ledger.verify() == []
