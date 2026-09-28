import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict

import pytest
from conftest import FIXTURE

from vader_intelligence.normalize import normalizer
from vader_intelligence.provenance import RunProvenanceV1
from vader_intelligence.replay import import_fixture, replay
from vader_intelligence.storage import Store, timestamp, writer_lock


def save_status(store, run, key, body=b'{"exchange_active":true}'):
    req = store.request(
        run, "status", key, "https://external-api.kalshi.com/trade-api/v2/exchange/status", {}
    )
    fetch = store.begin_fetch(req["id"], timestamp())
    error = store.complete_fetch(
        fetch,
        body=body,
        status=200,
        headers={},
        retrieved_at=timestamp(),
        elapsed=0,
        normalizer=normalizer("status"),
    )
    return req, fetch, error


def test_identical_payloads_preserve_new_observations(store, run):
    first = save_status(store, run, "first")[1]
    second = save_status(store, run, "second")[1]
    assert first != second
    assert store.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2
    assert store.verify_integrity() == []
    assert replay(store)["errors"] == []
    assert store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2


def test_fixture_import_replay_exact_and_offline(store, config):
    first = import_fixture(store, FIXTURE, config)
    before = [tuple(x) for x in store.db.execute("SELECT * FROM observations ORDER BY id")]
    second = import_fixture(store, FIXTURE, config)
    result = replay(store)
    assert first == second
    assert first["books"] == 1
    assert result["errors"] == []
    assert [tuple(x) for x in store.db.execute("SELECT * FROM observations ORDER BY id")] == before
    book = json.loads(
        store.db.execute("SELECT data_json FROM observations WHERE kind='book'").fetchone()[0]
    )
    assert book["yes_spread_dollars"] == "0.0100"
    assert book["yes_ask_size_fp"] == "5.75"
    assert book["game_id"] == 824948
    assert store.verify_integrity() == []


def test_bad_json_is_archived_without_checkpoint(store, run):
    req, fetch, error = save_status(store, run, "bad", b'{"bad":')
    assert error
    row = store.db.execute("SELECT * FROM fetches WHERE id=?", (fetch,)).fetchone()
    assert row["state"] == "parse_error"
    assert row["body_sha256"]
    assert store.db.execute("SELECT done FROM requests WHERE id=?", (req["id"],)).fetchone()[0] == 0


def test_parse_failure_rolls_back_partial_normalization(store, run):
    req = store.request(run, "status", "partial", "https://example.invalid", {})
    fetch = store.begin_fetch(req["id"], timestamp())

    def fail(db, fid, body):
        normalizer("status")(db, fid, body)
        raise ValueError("bad second record")

    err = store.complete_fetch(
        fetch,
        body=b"{}",
        status=200,
        headers={},
        retrieved_at=timestamp(),
        elapsed=0,
        normalizer=fail,
    )
    assert err
    assert store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1


def test_completed_fetch_cannot_be_overwritten(store, run):
    _, fetch, _ = save_status(store, run, "once")
    with pytest.raises(ValueError):
        store.complete_fetch(
            fetch, body=b"{}", status=200, headers={}, retrieved_at=timestamp(), elapsed=0
        )


def test_recovery_marks_pending_attempt_and_keeps_identity(store, run):
    req = store.request(run, "status", "crash", "https://example.invalid", {})
    first = store.begin_fetch(req["id"], timestamp())
    store.recover(run)
    second = store.begin_fetch(req["id"], timestamp())
    assert first != second
    assert [r[0] for r in store.db.execute("SELECT state FROM fetches ORDER BY seq")] == [
        "interrupted",
        "pending",
    ]


@pytest.mark.parametrize("after_commit", [False, True])
def test_hard_process_exit_at_commit_boundary(tmp_path, after_commit):
    path = str(tmp_path / "crash.sqlite3")
    script = """
import os, sys
from vader_intelligence.storage import Store, timestamp
from vader_intelligence.provenance import RunProvenanceV1
from vader_intelligence.normalize import normalizer
s=Store(sys.argv[1],min_free_bytes=1)
s.start_run(RunProvenanceV1('run','test','code',{}))
r=s.request('run','status','k','https://example.invalid',{})
f=s.begin_fetch(r['id'],timestamp())
def apply(db,fid,body):
    normalizer('status')(db,fid,body)
    if sys.argv[2]=='False': os._exit(7)
    return {}
s.complete_fetch(f,body=b'{}',status=200,headers={},retrieved_at=timestamp(),elapsed=0,normalizer=apply)
os._exit(7)
"""
    child = subprocess.run([sys.executable, "-c", script, path, str(after_commit)], timeout=10)
    assert child.returncode == 7
    s = Store(path, min_free_bytes=1)
    try:
        assert s.verify_integrity() == []
        assert s.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == int(after_commit)
        assert s.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == int(after_commit)
        assert s.db.execute("SELECT done FROM requests").fetchone()[0] == int(after_commit)
    finally:
        s.close()


def test_second_process_cannot_take_writer_lock(tmp_path):
    path = str(tmp_path / "locked.db")
    script = """
import sys
from vader_intelligence.storage import writer_lock
try:
    with writer_lock(sys.argv[1]): sys.exit(2)
except RuntimeError:
    sys.exit(0)
"""
    with writer_lock(path):
        child = subprocess.run([sys.executable, "-c", script, path], timeout=10)
        assert child.returncode == 0
    with writer_lock(path):
        assert os.path.exists(path + ".lockfile")


def test_migration_reopen_and_future_schema_guard(store, run, config):
    save_status(store, run, "seed")
    other = Store(config.database, min_free_bytes=1)
    assert other.db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    assert other.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    other.db.execute("PRAGMA user_version=3")
    other.close()
    with pytest.raises(RuntimeError, match="unsupported"):
        Store(config.database, min_free_bytes=1)


def test_provenance_roundtrip_does_not_invent_research_metadata():
    record = RunProvenanceV1("run", "collect", "code", {}, input_snapshot_ids=["f1"])
    data = json.loads(record.to_json())
    assert asdict(RunProvenanceV1(**data)) == asdict(record)
    assert data["model"] is None
    assert data["cost"] is None


def test_readonly_health_reports_low_space_without_writing(store, config):
    s = Store(config.database, readonly=True, min_free_bytes=10**30)
    try:
        assert s.health()["storage_pressure"]
        with pytest.raises(sqlite3.OperationalError):
            s.db.execute("DELETE FROM runs")
    finally:
        s.close()


@pytest.mark.parametrize("field", ["run_id", "started_at", "url"])
def test_fixture_ids_cannot_relabel_existing_provenance(
    store, config, fixture_data, tmp_path, field
):
    import_fixture(store, FIXTURE, config)
    if field == "run_id":
        fixture_data[field] += "-different"
    elif field == "url":
        fixture_data["responses"][0][field] = (
            "https://external-api.kalshi.com/trade-api/v2/exchange/status"
        )
    else:
        fixture_data["responses"][0][field] = "2026-09-27T16:00:00+00:00"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(fixture_data))
    with pytest.raises(ValueError, match="different bytes or provenance"):
        import_fixture(store, path, config)


def test_fixture_run_cannot_mask_latest_live_failure(store, config):
    store.start_run(RunProvenanceV1("live-failed", "collect", "code", {}))
    store.finish_run("live-failed", "failed", {"status": "failed"})
    import_fixture(store, FIXTURE, config)
    assert store.health()["latest_run"]["id"] == "live-failed"
    assert store.health()["latest_run"]["status"] == "failed"
