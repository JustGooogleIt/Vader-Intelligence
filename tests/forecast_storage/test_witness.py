"""Witness library (F3.3 plan §4.2–§4.4): cases C4, E1, E2, F1, F2, H1–H4, I1–I3, N13.

Every archive is disposable and every clock is injected, so every receipt here carries
synthetic lineage. The witness is never scheduled or called by the collector.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import Clock
from test_collector import handler_for
from test_transport import reader_for

from vader_intelligence.collector import Collector
from vader_intelligence.forecast import evidence, journal, schema
from vader_intelligence.forecast.witness import witness
from vader_intelligence.replay import import_fixture, replay
from vader_intelligence.settlement.schema import migrate as settlement_migrate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/mlb.json"


def collected_archive(store, config, fixture_data):
    """Schema 4 holding one complete two-phase collection (1 pass, 9 fetches, 2 books)."""
    settlement_migrate(store)
    schema.migrate(store.db)
    schema.migrate(store.db, record_contract=3)
    clock = Clock()
    handler, _, _ = handler_for(fixture_data)
    result = Collector(store, reader_for(store, config, handler, clock), config).run()
    assert result["status"] == "complete"
    clock.value += 120
    return store, clock, result


@pytest.fixture
def archive4(store, config, fixture_data):
    return collected_archive(store, config, fixture_data)


def steady_divergence():
    """A monotonic clock whose elapsed time disagrees with a frozen UTC by 3 s (>2 s),
    without ever reaching a realistic budget. First call is the session baseline."""
    state = {"n": 0}

    def mono():
        state["n"] += 1
        return 0.0 if state["n"] == 1 else 3.0

    return mono


def receipts(store):
    return [
        (row[0], json.loads(row[1]))
        for row in store.db.execute("SELECT id, payload_json FROM forecast_receipts ORDER BY id")
    ]


def test_witness_requires_record_contract_three(store, config, fixture_data):
    settlement_migrate(store)
    schema.migrate(store.db)
    clock = Clock()
    with pytest.raises(ValueError, match="record-contract 3"):
        witness(store, clock=clock.now, monotonic=clock.mono)
    assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0  # refused before a run


def test_one_receipt_per_subject_and_repeat_is_a_no_op(archive4):
    store, clock, result = archive4
    first = witness(store, clock=clock.now, monotonic=clock.mono)
    assert first["status"] == "complete" and first["errors"] == []
    assert first["receipts"] == 10 and first["passes_witnessed"] == 1
    assert first["fetches_witnessed"] == 9 and first["untrusted"] == 0
    assert first["injected_clock"] is True and first["pending_seen"] == 0
    rows = receipts(store)
    assert len(rows) == 10 and all(p["contract_version"] == 3 for _, p in rows)
    assert all(p["mode"] == "synthetic" and p["clock_status"] == "trusted" for _, p in rows)
    assert all(p["observed_at"] == rows[0][1]["observed_at"] for _, p in rows)  # one sample
    # Ordering (plan §8.3 step 7): observed after the subject, never after insertion.
    for _, p in rows:
        subject_time = (
            store.db.execute("SELECT retrieved_at FROM fetches WHERE id=?", (p["fetch_id"],))
            if p["subject_kind"] == "fetch"
            else store.db.execute(
                "SELECT json_extract(payload_json,'$.created_at') FROM discovery_passes WHERE id=?",
                (p["discovery_id"],),
            )
        ).fetchone()[0]
        assert subject_time <= p["observed_at"] <= p["created_at"]
    pass_receipt = next(p for _, p in rows if p["subject_kind"] == "discovery_pass")
    assert pass_receipt["discovery_id"] == result["discovery_pass_id"]
    assert pass_receipt["subject_digest"] == result["discovery_digest"]
    assert len(pass_receipt["references"]) == 1
    fetch_receipt = next(p for _, p in rows if p["subject_kind"] == "fetch")
    assert fetch_receipt["subject_digest"] == fetch_receipt["references"][0]["digest"]
    assert (
        fetch_receipt["subject_digest"]
        != fetch_receipt["references"][0]["fetches"][0]["raw_sha256"]
    )
    # Insert order for fetches is ascending seq.
    seqs = [
        store.db.execute("SELECT seq FROM fetches WHERE id=?", (p["fetch_id"],)).fetchone()[0]
        for _, p in rows
        if p["subject_kind"] == "fetch"
    ]
    assert seqs == sorted(seqs)
    clock.value += 60
    second = witness(store, clock=clock.now, monotonic=clock.mono)
    assert second["status"] == "complete" and second["receipts"] == 0
    assert receipts(store) == rows  # original observed_at kept
    assert store.verify_integrity() == []
    kinds = [r[0] for r in store.db.execute("SELECT kind FROM runs ORDER BY rowid")]
    assert kinds == ["collect", "forecast-witness", "forecast-witness"]


def test_interrupted_batch_resumes_at_the_later_real_time(archive4, monkeypatch):
    store, clock, _ = archive4
    original = store.append_forecast
    written = []

    def crash_after_three(table, payload):
        if len(written) == 3:
            raise KeyboardInterrupt()
        written.append(original(table, payload))
        return written[-1]

    monkeypatch.setattr(store, "append_forecast", crash_after_three)
    with pytest.raises(KeyboardInterrupt):
        witness(store, clock=clock.now, monotonic=clock.mono)
    monkeypatch.undo()
    first = receipts(store)
    assert len(first) == 3
    assert (
        store.db.execute("SELECT status FROM runs WHERE kind='forecast-witness'").fetchone()[0]
        == "running"
    )
    clock.value += 300
    result = witness(store, clock=clock.now, monotonic=clock.mono)
    assert result["status"] == "complete" and result["receipts"] == 7
    rows = receipts(store)
    assert rows[:3] == first  # earlier receipts unchanged
    later = {p["observed_at"] for _, p in rows[3:]}
    assert len(later) == 1 and later.pop() > first[0][1]["observed_at"]


def test_caps_and_budget_finish_partial_and_the_rest_is_picked_up_later(archive4):
    store, clock, _ = archive4
    result = witness(store, clock=clock.now, monotonic=clock.mono, max_subjects=4)
    assert result["status"] == "partial" and result["receipts"] == 5  # pass plus 4 newest fetches
    assert result["remaining"] == 5
    second = witness(store, clock=clock.now, monotonic=clock.mono, max_subjects=4)
    assert second["status"] == "partial" and second["receipts"] == 4
    third = witness(store, clock=clock.now, monotonic=clock.mono, max_subjects=4)
    assert third["status"] == "complete" and third["receipts"] == 1 and third["remaining"] == 0
    with pytest.raises(ValueError, match="budget"):
        witness(store, clock=clock.now, monotonic=clock.mono, budget=0)
    for name, bad in (("max_subjects", 0), ("scan_rows", 9999), ("max_passes", 101)):
        with pytest.raises(ValueError, match=name):
            witness(store, clock=clock.now, monotonic=clock.mono, **{name: bad})


def test_budget_already_spent_on_entry_writes_nothing(store, config, fixture_data):
    store, clock, _ = collected_archive(store, config, fixture_data)
    # A monotonic that is already past the deadline after the baseline sample.
    ticks = iter([0.0] + [100.0] * 50)
    result = witness(store, clock=clock.now, monotonic=lambda: next(ticks), budget=1.0)
    assert result["status"] == "partial" and result["receipts"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM forecast_receipts").fetchone()[0] == 0


def test_wall_clock_regression_between_runs_is_untrusted_and_unproven(archive4, config):
    store, clock, _ = archive4
    trusted = witness(store, clock=clock.now, monotonic=clock.mono)
    assert trusted["untrusted"] == 0
    # New evidence arrives, then the wall clock steps backward below the high-water mark.
    store.db.execute(
        "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,state,status,error)"
        " SELECT 'late-attempt', request_id, 9, started_at, started_at, 'error', NULL,"
        " 'ReadTimeout' FROM fetches ORDER BY seq LIMIT 1"
    )
    clock.value -= 100
    regressed = witness(store, clock=clock.now, monotonic=clock.mono)
    assert regressed["receipts"] == 1 and regressed["untrusted"] == 1
    assert regressed["last_untrusted"]["id"] == 11
    availability = evidence.fetch_availability(store.db, "late-attempt", mode="synthetic")
    assert availability.status == "clock_untrusted" and availability.observed_at is None
    assert availability.times.observed_at is None  # policy.freshness: availability_unproven
    # The first receipt for a subject is permanent: a later trusted run cannot repair it.
    clock.value += 1000
    repaired = witness(store, clock=clock.now, monotonic=clock.mono)
    assert repaired["receipts"] == 0
    assert evidence.fetch_availability(store.db, "late-attempt", mode="synthetic").status == (
        "clock_untrusted"
    )
    assert repaired["last_untrusted"]["id"] == 11  # anomaly stays visible


def test_utc_monotonic_divergence_within_a_session_is_untrusted(archive4):
    store, clock, _ = archive4
    frozen = clock.now()
    # UTC frozen while monotonic reports 3 s elapsed: a >2 s session divergence.
    result = witness(store, clock=lambda: frozen, monotonic=steady_divergence())
    assert result["receipts"] == 10 and result["untrusted"] == 10
    assert result["max_divergence_seconds"] > 2
    rows = receipts(store)
    assert all(p["clock_status"] == "untrusted" for _, p in rows)
    assert evidence.discovery_as_of(
        store.db, frozen + timedelta(seconds=1), mode="synthetic"
    ).reasons == ("discovery_missing",)


def test_restart_after_divergence_resets_only_the_monotonic_baseline(archive4):
    store, clock, _ = archive4
    trusted = witness(store, clock=clock.now, monotonic=clock.mono)
    assert trusted["untrusted"] == 0
    store.db.execute(
        "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,state,status,error)"
        " SELECT 'attempt-a', request_id, 9, started_at, started_at, 'error', NULL, 'x'"
        " FROM fetches ORDER BY seq LIMIT 1"
    )
    clock.value += 60
    frozen = clock.now()
    diverged = witness(store, clock=lambda: frozen, monotonic=steady_divergence())
    assert diverged["receipts"] == 1 and diverged["untrusted"] == 1
    store.db.execute(
        "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,state,status,error)"
        " SELECT 'attempt-b', request_id, 10, started_at, started_at, 'error', NULL, 'x'"
        " FROM fetches ORDER BY seq LIMIT 1"
    )
    clock.value += 60
    fresh = witness(store, clock=clock.now, monotonic=clock.mono)
    assert fresh["receipts"] == 1 and fresh["untrusted"] == 0  # N13
    assert fresh["last_untrusted"] == diverged["last_untrusted"]  # prior anomaly reported
    before = receipts(store)
    assert before[10][1]["clock_status"] == "untrusted"  # earlier receipt unchanged
    assert (
        evidence.fetch_availability(store.db, "attempt-b", mode="synthetic").status == "witnessed"
    )


def test_pending_and_interrupted_attempts_are_reported_never_witnessed(archive4, config):
    store, clock, result = archive4
    store.db.execute(
        "INSERT INTO fetches(id,request_id,attempt,started_at,state)"
        " SELECT 'killed', request_id, 9, started_at, 'pending' FROM fetches ORDER BY seq LIMIT 1"
    )
    first = witness(store, clock=clock.now, monotonic=clock.mono)
    assert first["pending_seen"] == 1 and first["interrupted_seen"] == 0
    assert not any(p["fetch_id"] == "killed" for _, p in receipts(store))
    store.recover(result["run_id"])  # I3: resume marks it interrupted
    second = witness(store, clock=clock.now, monotonic=clock.mono)
    assert second["pending_seen"] == 0 and second["interrupted_seen"] == 1
    assert second["receipts"] == 0
    assert evidence.fetch_availability(store.db, "killed", mode="synthetic").status == "incomplete"


def test_failed_attempt_without_body_is_witnessed_by_row_fingerprint(archive4):
    store, clock, _ = archive4
    store.db.execute(
        "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,state,status,error)"
        " SELECT 'timeout', request_id, 9, started_at, started_at, 'error', NULL, 'ReadTimeout'"
        " FROM fetches ORDER BY seq LIMIT 1"
    )
    witness(store, clock=clock.now, monotonic=clock.mono)
    receipt = next(p for _, p in receipts(store) if p["fetch_id"] == "timeout")
    assert receipt["references"][0]["fetches"][0]["raw_sha256"] is None
    assert receipt["subject_digest"] == receipt["references"][0]["digest"]
    assert evidence.fetch_availability(store.db, "timeout", mode="synthetic").status == "witnessed"


def test_fixture_lineage_stays_synthetic_and_replay_creates_no_receipt(store, config, fixture_data):
    settlement_migrate(store)
    schema.migrate(store.db)
    schema.migrate(store.db, record_contract=3)
    import_fixture(store, FIXTURE, config)  # H3: old-looking timestamps, fixture run
    clock = Clock()
    result = witness(store, clock=clock.now, monotonic=clock.mono)
    assert result["receipts"] == 7 and result["passes_witnessed"] == 0  # no summary imported
    assert all(p["mode"] == "synthetic" for _, p in receipts(store))
    assert evidence.discovery_as_of(store.db, clock.now(), mode="forward_shadow").kind is None
    assert evidence.discovery_as_of(store.db, clock.now(), mode="synthetic").kind is None
    before = receipts(store)
    assert replay(store)["forecast_storage"]["record_contracts"]["forecast_receipts"] == {
        "1": 0,
        "2": 0,
        "3": 7,
        "4": 0,
    }
    assert receipts(store) == before  # C4
    assert store.verify_integrity() == []


def test_witness_after_failed_collection_leaves_health_unhealthy(store, config, fixture_data):
    settlement_migrate(store)
    schema.migrate(store.db)
    schema.migrate(store.db, record_contract=3)
    handler, _, _ = handler_for(fixture_data)
    from conftest import response

    def failing(request):
        return response(503) if request.url.path.endswith("/markets") else handler(request)

    clock = Clock()
    failed = Collector(store, reader_for(store, config, failing, clock), config).run()
    assert failed["status"] == "failed"
    result = witness(store, clock=clock.now, monotonic=clock.mono)
    assert result["status"] == "complete" and result["passes_witnessed"] == 1
    health = store.health()
    assert health["latest_run"]["id"] == failed["run_id"]  # H4: witness never masks it
    assert health["latest_run"]["status"] == "failed" and health["stale"] is True
    assert health["latest_discovery"]["complete"] is False


def test_injected_clock_cannot_witness_a_live_pass(archive4):
    store, clock, _ = archive4
    from forecast_storage.fixtures import AT, HASH
    from vader_intelligence.provenance import RunProvenanceV1, source_hash

    store.db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "live",
            "collect",
            AT,
            AT,
            "complete",
            RunProvenanceV1("live", "collect", source_hash(), {"x": 1}).to_json(),
            "{}",
        ),
    )
    live_pass = {
        "schema_version": 1,
        "run_id": "live",
        "created_at": AT,
        "code_hash": source_hash(),
        "config_hash": HASH,
        "mode": "forward_shadow",
        "references": [],
        "session_id": "live-session",
        "phase_sequence": 1,
        "completed_at": AT,
        "complete": True,
        "reasons": [],
        "counts": {"games": 0, "contracts": 0},
        "eligibility_after_id": 0,
        "eligibility_through_id": 0,
        "manifest": {},
    }
    journal.append(store.db, "discovery_passes", live_pass)
    result = witness(store, clock=clock.now, monotonic=clock.mono)
    assert result["skipped_live_passes"] == 1 and result["passes_witnessed"] == 1
    assert (
        evidence.discovery_as_of(
            store.db, clock.now() + timedelta(seconds=1), mode="forward_shadow"
        ).kind
        is None
    )


def test_older_evidence_witnessed_today_cannot_prove_yesterday(archive4):
    store, clock, result = archive4
    clock.value += 86400
    witness(store, clock=clock.now, monotonic=clock.mono)
    yesterday = datetime(2026, 9, 27, 15, 1, tzinfo=timezone.utc)
    projection = evidence.discovery_as_of(store.db, yesterday, mode="synthetic")
    assert projection.kind is None and projection.reasons == ("discovery_missing",)
    fetch_id = store.db.execute("SELECT id FROM fetches ORDER BY seq LIMIT 1").fetchone()[0]
    availability = evidence.fetch_availability(store.db, fetch_id, mode="synthetic")
    assert availability.status == "witnessed" and availability.observed_at > yesterday
    from vader_intelligence.forecast.policy import freshness

    assert freshness(availability.times, yesterday).reasons == ("availability_after_cutoff",)
