"""Combined collector/settlement/operations checks; all inputs are synthetic."""

import hashlib
import json
import sys
from pathlib import Path

import pytest
from conftest import Clock, response
from test_collector import handler_for
from test_transport import reader_for

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import archive  # noqa: E402
from vader_intelligence.collector import Collector  # noqa: E402
from vader_intelligence.replay import replay  # noqa: E402
from vader_intelligence.settlement import journal  # noqa: E402
from vader_intelligence.settlement.schema import migrate  # noqa: E402
from vader_intelligence.settlement.service import SettlementRefresh, inspect  # noqa: E402
from vader_intelligence.storage import Store  # noqa: E402


def rows(db):
    tables = sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    return {t: db.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}


def snapshot(db):
    return {t: [tuple(r) for r in rs] for t, rs in rows(db).items()}


def test_both_schemas_collect_replay_backup_restore(
    store, config, fixture_data, tmp_path, monkeypatch
):
    handler, _, event = handler_for(fixture_data)
    reader = reader_for(store, config, handler)
    collected = Collector(store, reader, config).run()
    assert (
        archive.classify({"database": str(store.path)}, collected, 0) == "collected_eligible_games"
    )
    first = snapshot(store.db)
    archive.backup(store.path, tmp_path / "backup1.sqlite3")
    archive.backup(tmp_path / "backup1.sqlite3", tmp_path / "restored1.sqlite3")
    restored = Store(tmp_path / "restored1.sqlite3", min_free_bytes=1)
    try:
        assert snapshot(restored.db) == first
        assert replay(restored)["status"] == "complete"
        assert snapshot(restored.db) == first
    finally:
        restored.close()
    migrate(store)
    second = snapshot(store.db)
    for table in first:
        if table != "schema_migrations":
            assert first[table] == second[table]
    collected = Collector(store, reader, config).run()
    assert collected["status"] == "complete"
    assert (
        archive.classify({"database": str(store.path)}, collected, 0) == "collected_eligible_games"
    )
    market = dict(
        event["markets"][0],
        status="finalized",
        result="yes",
        settlement_value_dollars="1.0000",
        settlement_ts="2026-09-28T01:00:00Z",
    )
    pdf = b"%PDF-1.4\nsynthetic fixture\n%%EOF"
    monkeypatch.setattr(journal, "REVIEWED_TERMS", {hashlib.sha256(pdf).hexdigest()})

    def settlement_handler(request):
        if request.url.path.endswith("/markets/" + market["ticker"]):
            return response(data={"market": market})
        return handler(request)

    settlement_reader = reader_for(store, config, settlement_handler)
    for payout, outcome in [("1.0000", "yes"), ("0.5300", "scalar"), ("0.5300", "scalar")]:
        market.update(settlement_value_dollars=payout, result=outcome)
        result = SettlementRefresh(store, settlement_reader, config).run(tickers=[market["ticker"]])
        assert result["status"] == "complete", result
    assert store.health()["latest_run"]["id"] == collected["run_id"]
    before = snapshot(store.db)
    backup = archive.backup(store.path, tmp_path / "backup2.sqlite3")
    assert backup["schema_version"] == 2
    assert backup["counts"]["settlement_versions"] > 0
    assert backup["counts"]["settlement_observations"] > backup["counts"]["settlement_versions"]
    archive.backup(tmp_path / "backup2.sqlite3", tmp_path / "restored2.sqlite3")
    restored = Store(tmp_path / "restored2.sqlite3", min_free_bytes=1)
    try:
        assert snapshot(restored.db) == before  # Every table, row, checkpoint and raw relationship.
        assert inspect(restored)["records"][0]["data"]["payout_kind"] == "exceptional"
        for _ in range(2):
            assert replay(restored)["status"] == "complete"
            assert snapshot(restored.db) == before
        assert restored.verify_integrity() == []
    finally:
        restored.close()
        reader.close()
        settlement_reader.close()


@pytest.mark.parametrize("version", [1, 2])
def test_resumed_empty_pass_not_contaminated_by_old_eligibility(
    store, config, fixture_data, version
):
    if version == 2:
        migrate(store)
    handler, _, _ = handler_for(fixture_data)
    books = 0

    def interrupted(request):
        nonlocal books
        if request.url.path.endswith("/orderbook"):
            books += 1
            if books == 2:
                raise KeyboardInterrupt
        return handler(request)

    with pytest.raises(KeyboardInterrupt):
        Collector(store, reader_for(store, config, interrupted), config).run()
    run = store.db.execute("SELECT id FROM runs").fetchone()[0]
    handler, _, _ = handler_for(fixture_data, no_games=True)
    result = Collector(store, reader_for(store, config, handler), config).run(resume=run)
    assert result["books"] == 1 and result["books_this_pass"] == 0
    assert (
        result["discovery_complete"]
        and result["eligible_contracts"] == result["eligible_games"] == 0
    )
    assert (
        archive.classify({"database": str(store.path)}, result, 3) == "discovery_no_eligible_games"
    )
    # A successful settlement run never satisfies the collection-specific predicate.
    with store.transaction():
        store.db.execute("UPDATE runs SET kind='settlement-refresh' WHERE id=?", (run,))
    assert archive.classify({"database": str(store.path)}, result, 3) == "unverified_result"


def test_missing_candidates_are_not_reported_as_empty_success(store, config, fixture_data):
    handler, _, event = handler_for(fixture_data)
    original = json.loads(json.dumps(event))

    def missing(request):
        if request.url.path.endswith("/markets"):
            return response(data={"markets": original["markets"], "cursor": ""})
        return handler(request)

    for market in event["markets"]:
        market["ticker"] += "-DIFFERENT"
    result = Collector(store, reader_for(store, config, missing), config).run()
    assert result["status"] == "partial" and not result["discovery_complete"]
    assert archive.classify({"database": str(store.path)}, result, 1) == "failed_or_partial"


@pytest.mark.parametrize("version", [-1, 3, 999])
def test_future_schema_rejected_everywhere_without_mutation(store, config, tmp_path, version):
    store.db.execute(f"PRAGMA user_version={version}")
    for readonly in (False, True):
        with pytest.raises(RuntimeError, match="unsupported"):
            Store(config.database, min_free_bytes=1, readonly=readonly)
    with pytest.raises(ValueError, match="supported"):
        archive.inspect(store.path)
    with pytest.raises(ValueError, match="supported"):
        archive.backup(store.path, tmp_path / "future.sqlite3")
    assert not (tmp_path / "future.sqlite3").exists()
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == version


def test_cutoff_crossing_is_not_empty_discovery(store, config, fixture_data):
    handler, _, _ = handler_for(fixture_data)
    clock = Clock()
    clock.origin = clock.origin.replace(hour=19, minute=2, second=50)
    collector = Collector(store, reader_for(store, config, handler, clock), config)
    original = collector.record_eligibility

    def slow_commit(*args):
        from datetime import timedelta

        original(*args)
        clock.origin += timedelta(hours=1)

    collector.record_eligibility = slow_commit
    result = collector.run()
    assert result["discovery_complete"] and result["eligible_contracts"] == 1
    assert result["books_this_pass"] == 0 and result["status"] == "inconclusive"
    assert (
        archive.classify({"database": str(store.path)}, result, 3)
        == "inconclusive_with_eligible_games"
    )


def test_failed_discovery_does_not_claim_empty_universe(store, config, fixture_data):
    handler, _, _ = handler_for(fixture_data)

    def failing(request):
        return response(503) if request.url.path.endswith("/markets") else handler(request)

    result = Collector(store, reader_for(store, config, failing), config).run()
    assert result["status"] == "failed" and result["discovery_complete"] is False
    assert archive.classify({"database": str(store.path)}, result, 1) == "failed_or_partial"


def test_both_writer_clis_refuse_existing_lock(store):
    import subprocess

    from vader_intelligence.storage import writer_lock

    before = snapshot(store.db)
    with writer_lock(store.path):
        for command in (["db-init"], ["settlement", "migrate"]):
            result = subprocess.run(
                [sys.executable, "-m", "vader_intelligence.cli", "--db", str(store.path), *command],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 1 and "writer lock" in result.stderr
    assert snapshot(store.db) == before
