"""Regression checks for the six defects found by the independent review of PR #10.

Each case reproduces the reviewer's adversarial scenario (disposable synthetic archives,
no network) and asserts the repaired behaviour:

1. Synthetic collection fetches never become forward evidence.
2. Supplied journal assertions never qualify as operational availability proof.
3. Invalid contract archives are rejected before any mutation and never migrated.
4. Scored unpinned decisions cannot bypass cohort isolation.
5. Valid legacy reconstruction records never obstruct witnessing.
6. Witness discovery-pass limits apply per invocation.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest
from conftest import Clock
from test_collector import handler_for
from test_transport import reader_for

from vader_intelligence.collector import Collector
from vader_intelligence.config import Config
from vader_intelligence.forecast import evidence, journal, schema
from vader_intelligence.forecast.references import digest, reference, run_source_mode
from vader_intelligence.forecast.witness import witness
from vader_intelligence.provenance import RunProvenanceV1, json_text
from vader_intelligence.settlement.schema import migrate as settlement_migrate
from vader_intelligence.storage import Store, writer_lock

from .fixtures import AT, HASH, legacy, populated, snapshot
from .test_evidence import NS, T0, Archive, iso
from .test_record_contract import (
    WITNESS_RUN,
    evaluation_for,
    fetch_receipt,
    pinned_decision,
    witness_run,
)

FIXTURE = __import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures/mlb.json"


# ------------------------------------------------------------------ finding 1


def test_synthetic_collector_fetches_never_become_forward_evidence(tmp_path):
    """The real Collector with its mocked reader, then the real witness with the system
    clock: every receipt is synthetic and the forward projection sees nothing."""
    path = tmp_path / "synthetic-collection.sqlite3"
    cfg = Config(database=str(path), min_free_bytes=1)
    with writer_lock(path):
        store = Store(path, min_free_bytes=1)
        settlement_migrate(store)
        schema.migrate(store.db)
        schema.migrate(store.db, record_contract=3)
        handler, _, _ = handler_for(json.loads(FIXTURE.read_text()))
        reader = reader_for(store, cfg, handler, Clock())
        assert reader.live is False
        collected = Collector(store, reader, cfg).run()
        reader.close()
        assert collected["status"] == "complete"
        # Lineage is persisted in the run's provenance, not inferred from the clock.
        assert run_source_mode(store.db, collected["run_id"]) == "synthetic"
        provenance = json.loads(
            store.db.execute(
                "SELECT provenance_json FROM runs WHERE id=?", (collected["run_id"],)
            ).fetchone()[0]
        )
        assert provenance["tool_configuration"]["source_lineage"] == "synthetic"
        observed = witness(store)  # real local clocks only; no network; synthetic inputs
        assert observed["status"] == "complete" and observed["source_lineage"] == "live"
        modes = {
            row[0]
            for row in store.db.execute(
                "SELECT DISTINCT json_extract(payload_json,'$.mode') FROM forecast_receipts"
            )
        }
        assert modes == {"synthetic"}
        fid = store.db.execute(
            "SELECT f.id FROM fetches f JOIN requests r ON r.id=f.request_id"
            " WHERE r.stage='book' LIMIT 1"
        ).fetchone()[0]
        assert reference(store.db, "fetches", {"id": fid})["source_mode"] == "synthetic"
        forward = evidence.fetch_availability(store.db, fid)  # default mode forward_shadow
        assert forward.status == "availability_unproven"
        assert evidence.fetch_availability(store.db, fid, mode="synthetic").status == "witnessed"
        assert evidence.discovery_as_of(store.db, T0 + timedelta(days=30)).kind is None
        assert store.verify_integrity() == []
        store.close()


def test_resume_cannot_change_a_run_source_lineage(store, config, fixture_data):
    settlement_migrate(store)
    schema.migrate(store.db)
    handler, _, _ = handler_for(fixture_data)
    reader = reader_for(store, config, handler, Clock())
    collected = Collector(store, reader, config).run()
    store.db.execute("UPDATE runs SET status='partial' WHERE id=?", (collected["run_id"],))
    from vader_intelligence.transport import Reader

    live_reader = Reader(store, config)  # live lineage: injected nothing
    try:
        assert live_reader.live is True
        with pytest.raises(ValueError, match="source lineage"):
            Collector(store, live_reader, config).run(resume=collected["run_id"])
    finally:
        live_reader.close()
        reader.close()


@pytest.mark.parametrize("kind", ["forecast-storage", "history-import", "test"])
def test_run_without_lineage_or_protocol_is_not_live(tmp_path, kind):
    """Stack review R2: a run that records no source lineage and is not a collector or
    settlement run of the code that predates lineage (an import, a test harness, any
    future run kind) carries no evidence of being live. It fails closed: its fetches are
    synthetic evidence and can never be witnessed as forward evidence."""
    path = tmp_path / "r2.sqlite3"
    a = Archive(path)
    a.run("unlabelled", kind=kind, protocol=None)
    fetch_id, _ = a.fetch("unlabelled", "markets", T0)
    a.db.close()
    with writer_lock(path):
        store = Store(path, min_free_bytes=1)
        try:
            assert run_source_mode(store.db, "unlabelled") == "synthetic"
            assert reference(store.db, "fetches", {"id": fetch_id})["source_mode"] == "synthetic"
            observed = witness(store)  # the system clock: a live witness run
            assert observed["status"] == "complete" and observed["source_lineage"] == "live"
            forward = evidence.fetch_availability(store.db, fetch_id)
            assert forward.status == "availability_unproven"
            synthetic = evidence.fetch_availability(store.db, fetch_id, mode="synthetic")
            assert synthetic.status == "witnessed"
        finally:
            store.close()


@pytest.mark.parametrize(
    "kind, protocol",
    [
        ("collect", None),
        ("discover", None),
        ("live-check", None),
        ("collect", "two-phase-v1"),
        ("discover", "two-phase-v1"),
        ("live-check", "two-phase-v1"),
        ("settlement-refresh", None),
    ],
)
def test_pre_lineage_collection_runs_keep_their_classification(tmp_path, kind, protocol):
    """The runs older code wrote without a lineage field are real collections: the
    collector's discover/collect/live-check runs (interleaved or two-phase) and the
    settlement refresh. They stay live unless their own discovery summary says synthetic,
    so typed references already stored for their evidence keep their digests."""
    a = Archive(tmp_path / "pre-lineage.sqlite3")
    a.run("old", kind=kind, protocol=protocol)
    try:
        assert run_source_mode(a.db, "old") == "live"
        if protocol is not None:
            a.pass_("old", T0, mode="synthetic")
            assert run_source_mode(a.db, "old") == "synthetic"
    finally:
        a.db.close()


# ------------------------------------------------------------------ finding 2


def test_supplied_receipts_are_rejected_and_never_operational_proof(tmp_path):
    a = Archive(tmp_path / "supplied.sqlite3")
    a.run("collector")
    fetch_id, seq = a.fetch("collector", "status", T0 - timedelta(seconds=20))
    pass_id = a.pass_("collector", T0 - timedelta(seconds=10), through=seq, mode="forward_shadow")
    a.run(
        "ordinary-journal-caller", kind="forecast-storage", protocol=None, configuration={"ns": NS}
    )

    def supplied(**subject):
        table = "fetches" if "fetch_id" in subject else "discovery_passes"
        key = subject.get("fetch_id") or subject.get("pass_id")
        ref = reference(a.db, table, {"id": key})
        subject_digest = (
            ref["digest"]
            if table == "fetches"
            else a.db.execute("SELECT digest FROM discovery_passes WHERE id=?", (key,)).fetchone()[
                0
            ]
        )
        return dict(
            schema_version=1,
            contract_version=3,
            run_id="ordinary-journal-caller",
            created_at=iso(T0),
            code_hash=HASH,
            config_hash=digest({"ns": NS}),
            mode="forward_shadow",
            references=[ref],
            subject_kind="fetch" if table == "fetches" else "discovery_pass",
            fetch_id=subject.get("fetch_id"),
            binding_id=None,
            publication_id=None,
            discovery_id=subject.get("pass_id"),
            subject_digest=subject_digest,
            observed_at=iso(T0),
            session_id="supplied",
            observer_namespace=NS,
            monotonic_offset="0",
            clock_status="trusted",
        )

    before = snapshot(a.db)
    for kwargs in ({"fetch_id": fetch_id}, {"pass_id": pass_id}):
        with pytest.raises(ValueError, match="operational witness run"):
            journal.append(a.db, "forecast_receipts", supplied(**kwargs))
        # The frozen contract 2 accepts no new row at all (decision D-001).
        with pytest.raises(ValueError, match="frozen"):
            journal.append(a.db, "forecast_receipts", supplied(**kwargs) | {"contract_version": 2})
    assert snapshot(a.db) == before
    assert evidence.fetch_availability(a.db, fetch_id).status == "availability_unproven"
    assert evidence.discovery_as_of(a.db, T0).universe_complete is False
    # A witness run that names another namespace or session cannot write this one.
    a.witness_run("other-witness", namespace="other-ns")
    # (mode synthetic: that witness run has synthetic lineage, so only the namespace
    # boundary is under test here)
    impostor = supplied(fetch_id=fetch_id) | {
        "run_id": "other-witness",
        "session_id": "witness-session",
        "mode": "synthetic",
    }
    with pytest.raises(ValueError, match="operational witness run"):
        journal.append(a.db, "forecast_receipts", impostor)
    a.db.close()


@pytest.mark.parametrize("contract", [2, 3])
def test_projection_ignores_a_supplied_receipt_present_in_a_malformed_archive(tmp_path, contract):
    """Defense in depth: a row inserted around the journal (triggers dropped) is ignored by
    the projection and the witness. Integrity names it: a contract-3 row is a failure; a
    contract-2 row is valid only under the frozen draft rules, so it is reported as a
    legacy row with its current-rule reason and is never proof (decision D-001)."""
    a = Archive(tmp_path / "malformed.sqlite3")
    a.run("collector")
    fetch_id, _ = a.fetch("collector", "status", T0 - timedelta(seconds=20))
    a.run(
        "ordinary-journal-caller", kind="forecast-storage", protocol=None, configuration={"ns": NS}
    )
    ref = reference(a.db, "fetches", {"id": fetch_id})
    payload = dict(
        schema_version=1,
        contract_version=contract,
        run_id="ordinary-journal-caller",
        created_at=iso(T0),
        code_hash=HASH,
        config_hash=digest({"ns": NS}),
        mode="forward_shadow",
        references=[ref],
        subject_kind="fetch",
        fetch_id=fetch_id,
        binding_id=None,
        publication_id=None,
        discovery_id=None,
        subject_digest=ref["digest"],
        observed_at=iso(T0),
        session_id="supplied",
        observer_namespace=NS,
        monotonic_offset="0",
        clock_status="trusted",
    )
    from vader_intelligence.forecast import contracts

    encoded = contracts.validate("forecast_receipts", payload)
    a.db.execute(
        "INSERT INTO forecast_receipts(idempotency_key,run_id,subject_kind,fetch_id,binding_id,"
        "publication_id,discovery_id,observed_at,digest,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            contracts.key("forecast_receipts", payload),
            payload["run_id"],
            "fetch",
            fetch_id,
            None,
            None,
            None,
            payload["observed_at"],
            digest(payload),
            encoded,
        ),
    )
    assert evidence.fetch_availability(a.db, fetch_id).status == "availability_unproven"
    assert evidence.discovery_as_of(a.db, T0).diagnostics["supplied_receipts_ignored"] == 1
    if contract == 3:
        with pytest.raises(ValueError, match="operational witness run"):
            journal.integrity(a.db)
    else:
        result = journal.integrity(a.db)
        assert result["classification"]["forecast_receipts"]["legacy_draft"] == 1
        assert result["admissibility"] == "legacy_rows_present"
        (example,) = result["legacy_examples"]
        assert example["contract"] == 2 and "operational witness run" in example["reason"]
    a.db.close()


# ------------------------------------------------------------------ finding 3


def invalid_archive(path, corruption):
    db, p = populated(path)
    if corruption == "contract3_on_3":
        witness_run(db, WITNESS_RUN, {}, "synthetic")
        schema.migrate(db, record_contract=3)
        journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-2"))
        db.execute("DELETE FROM schema_migrations WHERE version IN (4, 5)")
        db.execute("PRAGMA user_version=3")
    else:
        db.execute("DROP TRIGGER forecast_receipts_no_update")
        raw = json.loads(
            db.execute("SELECT payload_json FROM forecast_receipts WHERE id=1").fetchone()[0]
        )
        raw["contract_version"] = 99
        db.execute(
            "UPDATE forecast_receipts SET payload_json=?,digest=? WHERE id=1",
            (json_text(raw), digest(raw)),
        )
        db.execute(
            "CREATE TRIGGER forecast_receipts_no_update BEFORE UPDATE ON forecast_receipts"
            " BEGIN SELECT RAISE(ABORT,'immutable'); END"
        )
    assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    db.close()


@pytest.mark.parametrize("corruption", ["contract3_on_3", "unknown_contract"])
@pytest.mark.parametrize(
    "entry", ["store", "readonly_store", "cli_init", "cli_replay", "cli_migrate"]
)
def test_invalid_contract_rejected_without_mutation(tmp_path, corruption, entry):
    path = tmp_path / "invalid.sqlite3"
    invalid_archive(path, corruption)
    before = path.read_bytes()
    if entry in ("store", "readonly_store"):
        with pytest.raises((ValueError, RuntimeError, sqlite3.Error), match="contract"):
            Store(path, min_free_bytes=0, readonly=entry == "readonly_store")
    else:
        cmd = {
            "cli_init": ["db-init"],
            "cli_replay": ["replay"],
            "cli_migrate": ["forecast", "migrate", "--record-contract", "3"],
        }[entry]
        result = subprocess.run(
            [sys.executable, "-m", "vader_intelligence.cli", "--db", str(path), *cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1 and "contract" in result.stderr
    assert path.read_bytes() == before
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    db.close()
    assert not (tmp_path / "invalid.sqlite3-wal").exists()


def test_rejected_schema2_contract_migration_leaves_delete_mode(tmp_path):
    path = tmp_path / "schema2.sqlite3"
    db = legacy(path)
    db.close()
    before = path.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vader_intelligence.cli",
            "--db",
            str(path),
            "forecast",
            "migrate",
            "--record-contract",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1 and "requires schema 3" in result.stderr
    assert path.read_bytes() == before
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()


def test_005_failure_rolls_back_then_retry_preserves_rows(tmp_path, monkeypatch):
    db, p = populated(tmp_path / "migration.sqlite3")
    before = snapshot(db)
    original = schema.check_schema

    def fail_after_version_change(conn):
        original(conn)
        if conn.execute("PRAGMA user_version").fetchone()[0] == 5:
            raise ValueError("injected failure after version write")

    monkeypatch.setattr(schema, "check_schema", fail_after_version_change)
    with pytest.raises(ValueError, match="injected failure"):
        schema.migrate(db, record_contract=3)
    # Both markers (004 and 005) roll back together: still schema 3, ledger [1, 2, 3].
    assert snapshot(db) == before and db.execute("PRAGMA user_version").fetchone()[0] == 3
    monkeypatch.setattr(schema, "check_schema", original)
    assert schema.migrate(db, record_contract=3)
    after = snapshot(db)
    assert all(after[t] == rows for t, rows in before.items() if t != "schema_migrations")
    assert [r[0] for r in after["schema_migrations"]] == [1, 2, 3, 4, 5]
    assert schema.migrate(db, record_contract=3) is False
    assert snapshot(db) == after
    db.close()


# ------------------------------------------------------------------ finding 4


def test_scored_unpinned_rows_cannot_bypass_cohort_check(tmp_path):
    db, p = populated(tmp_path / "scored-cohort.sqlite3")
    schema.migrate(db, record_contract=3)
    decisions, hashes = [], []
    for n in (1, 2):
        rid = f"collector-{n}"
        cfg = {"max_pages": n}
        ch = digest(cfg)
        hashes.append(ch)
        db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            (
                rid,
                "collect",
                AT,
                AT,
                "complete",
                RunProvenanceV1(rid, "collect", HASH, cfg).to_json(),
                "{}",
            ),
        )
        passed = p["discovery_passes"] | dict(run_id=rid, session_id=f"pass-{n}", config_hash=ch)
        pid = journal.append(db, "discovery_passes", passed)
        d = pinned_decision(db, p, pid, game_id=900001 + n)
        d["discovery_id"] = None
        d["manifest"] = {"source_pass": reference(db, "discovery_passes", {"id": pid})}
        did = journal.append(db, "forecast_decisions", d)
        decisions.append((did, d))
    settled = dict(
        status="finalized",
        result="yes",
        market_type="binary",
        notional_value_dollars="1.0000",
        settlement_value_dollars="1.0000",
        settlement_ts=AT,
    )
    db.execute(
        "INSERT INTO settlement_versions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            4,
            "kalshi",
            "SCORED-SYNTHETIC",
            1,
            None,
            digest(settled),
            json_text(settled),
            "finalized",
            "fetch-1",
        ),
    )
    report, items = evaluation_for(db, p, decisions)
    for item in items:
        item.update(
            kalshi_id=4,
            y=1,
            payout="1.0000",
            outcome_status="eligible",
            outcome_reasons=[],
            scores={"constant-v1": {"brier": "0.25", "log_loss": "0.693147"}, "midpoint-v1": None},
            references=[reference(db, "settlement_versions", {"id": 4})],
        )
    report["report"]["items_digest"] = digest(items)
    before = snapshot(db)
    with pytest.raises(
        ValueError, match="without discovery provenance|without a stored cutoff probability"
    ):
        journal.append_evaluation(db, report, items)
    assert snapshot(db) == before
    # The same rows as honest unscored coverage are accepted, with the unpinned count visible.
    for item in items:
        item.update(scores={"constant-v1": None, "midpoint-v1": None}, y=None, payout=None)
    report["report"]["items_digest"] = digest(items)
    evaluation_id = journal.append_evaluation(db, report, items)
    assert journal.check_cohort(db, items) == {
        "record_contract": 3,
        "collector_config_hash": None,
        "unpinned_decisions": 2,
    }
    assert evaluation_id and journal.integrity(db)["integrity"] == "verified"
    db.close()


# ------------------------------------------------------------------ finding 5


def test_legacy_reconstruction_pass_does_not_poison_witness(tmp_path):
    path = tmp_path / "legacy-reconstruction.sqlite3"
    a = Archive.__new__(Archive)
    a.db = legacy(path)
    a.seq = 0
    schema.migrate(a.db)
    a.run("research-import", kind="forecast-storage", protocol=None)
    a.pass_("research-import", T0, mode="historical_reconstruction")
    assert journal.integrity(a.db)["integrity"] == "verified"
    schema.migrate(a.db, record_contract=3)
    a.db.close()
    with writer_lock(path):
        store = Store(path, min_free_bytes=0)
        first = witness(store)
        second = witness(store)
        store.close()
    assert first["status"] == "complete" and first["errors"] == []
    assert first["skipped_legacy_subjects"] == 1 and first["fetches_witnessed"] == 3
    assert second["status"] == "complete" and second["skipped_legacy_subjects"] == 1
    assert second["receipts"] == 0


# ------------------------------------------------------------------ finding 6


def test_witness_pass_limit_is_per_invocation(tmp_path):
    path = tmp_path / "pass-limit.sqlite3"
    a = Archive(path)
    a.run("collector")
    for n in range(3):
        a.pass_("collector", T0, session=f"pass-{n}")
    a.db.close()
    with writer_lock(path):
        store = Store(path, min_free_bytes=0)
        first = witness(
            store, clock=lambda: T0 + timedelta(seconds=60), monotonic=lambda: 0.0, max_passes=1
        )
        second = witness(
            store, clock=lambda: T0 + timedelta(seconds=61), monotonic=lambda: 0.0, max_passes=1
        )
        third = witness(
            store, clock=lambda: T0 + timedelta(seconds=62), monotonic=lambda: 0.0, max_passes=5
        )
        store.close()
    assert first["passes_witnessed"] == 1 and first["status"] == "partial"
    assert first["remaining_by_kind"]["discovery_pass"] == 2 and first["remaining"] == 2
    assert second["passes_witnessed"] == 1 and second["status"] == "partial"
    assert third["passes_witnessed"] == 1 and third["status"] == "complete"
    assert third["remaining"] == 0


def test_witness_fetch_limit_is_per_invocation(tmp_path):
    path = tmp_path / "fetch-limit.sqlite3"
    a = Archive(path)
    a.run("collector")
    for _ in range(5):
        a.fetch("collector", "status", T0 - timedelta(seconds=20))
    a.db.close()
    with writer_lock(path):
        store = Store(path, min_free_bytes=0)
        first = witness(
            store, clock=lambda: T0 + timedelta(seconds=60), monotonic=lambda: 0.0, max_subjects=2
        )
        store.close()
    assert first["fetches_witnessed"] == 2 and first["status"] == "partial"
    # Three fixture fetches from the legacy base plus five new ones, two witnessed.
    assert first["remaining_by_kind"]["fetch"] == 6
