"""Real Unix Store/CLI/lock/replay integration; never replaced by Windows shims."""

import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("requires real Unix fcntl/transport; run on Mac", allow_module_level=True)

from ops import archive  # noqa: E402
from vader_intelligence.cli import execute, parser  # noqa: E402
from vader_intelligence.config import Config  # noqa: E402
from vader_intelligence.forecast import journal, schema  # noqa: E402
from vader_intelligence.provenance import RunProvenanceV1, source_hash  # noqa: E402
from vader_intelligence.replay import import_fixture, replay  # noqa: E402
from vader_intelligence.settlement.schema import migrate as settlement_migrate  # noqa: E402
from vader_intelligence.storage import Store, writer_lock  # noqa: E402

from .fixtures import populated, snapshot  # noqa: E402
from .test_p2_regressions import old_layout  # noqa: E402


def test_real_explicit_1_2_3_and_no_implicit_upgrade(tmp_path):
    path = tmp_path / "native.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)
    store = Store(path, min_free_bytes=1)
    try:
        fixture = Path(__file__).resolve().parents[1] / "fixtures/mlb.json"
        import_fixture(store, fixture, config)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
        with pytest.raises(ValueError, match="schema 2"):
            schema.migrate(store.db)
        before = snapshot(store.db)
        with writer_lock(path):
            settlement_migrate(store)
            schema.migrate(store.db)
        for table, values in before.items():
            after = snapshot(store.db)[table]
            assert values == (after[:1] if table == "schema_migrations" else after)
        stable = snapshot(store.db)
        settlement_migrate(store)  # Must not downgrade schema 3.
        assert snapshot(store.db) == stable
        result = replay(store)
        assert result["forecast_storage"]["replay"] == "preserved_not_reconstructed"
        assert snapshot(store.db) == stable
        store.start_run(RunProvenanceV1("failed-collector", "collect", source_hash(), {}))
        store.finish_run("failed-collector", "failed", {})
        store.start_run(RunProvenanceV1("research", "forecast-storage", source_hash(), {}))
        store.finish_run("research", "complete", {})
        assert store.health()["latest_run"]["id"] == "failed-collector"
    finally:
        store.close()
    result = execute(parser().parse_args(["--db", str(path), "forecast", "migrate"]))
    assert result["schema_version"] == 3 and result["migrated"] is False
    assert (
        execute(parser().parse_args(["--db", str(path), "settlement", "migrate"]))["schema_version"]
        == 3
    )


def test_populated_forecast_restore_replay_preserves_history(tmp_path):
    source = tmp_path / "full.sqlite3"
    db, _ = populated(source)
    db.close()
    store = Store(source, min_free_bytes=1)
    try:
        # First legacy replay may add missing legacy normalizations, never forecast data.
        forecasts = {t: snapshot(store.db)[t] for t in schema.COLUMNS}
        replay(store)
        assert {t: snapshot(store.db)[t] for t in schema.COLUMNS} == forecasts
        before = snapshot(store.db)
        archive.backup(source, tmp_path / "backup.sqlite3")
        archive.backup(tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3")
    finally:
        store.close()
    restored = Store(tmp_path / "restored.sqlite3", min_free_bytes=1)
    try:
        for _ in range(2):
            result = replay(restored)
            assert result["forecast_storage"]["materialization"] == "not_implemented"
            assert snapshot(restored.db) == before
        assert journal.integrity(restored.db)["integrity"] == "verified"
    finally:
        restored.close()


def test_real_cli_writer_exclusion(tmp_path):
    path = tmp_path / "locked.sqlite3"
    store = Store(path, min_free_bytes=1)
    settlement_migrate(store)
    store.close()
    with writer_lock(path):
        with pytest.raises(RuntimeError, match="writer lock"):
            execute(parser().parse_args(["--db", str(path), "forecast", "migrate"]))
    ro = Store(path, min_free_bytes=1, readonly=True)
    assert ro.db.execute("PRAGMA user_version").fetchone()[0] == 2
    ro.close()


def test_store_and_cli_refuse_pre_fix_schema_three(tmp_path):
    source, payloads = populated(tmp_path / "fixed.sqlite3")
    source.close()
    path = tmp_path / "pre-fix.sqlite3"
    old = old_layout(path, payloads)
    try:
        before = snapshot(old)
        ddl = old.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
        for readonly in (True, False):
            with pytest.raises(ValueError, match="unsupported schema-3 layout"):
                Store(path, min_free_bytes=1, readonly=readonly)
        with pytest.raises(ValueError, match="unsupported schema-3 layout"):
            execute(parser().parse_args(["--db", str(path), "forecast", "migrate"]))
        assert snapshot(old) == before
        assert old.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall() == ddl
    finally:
        old.close()
