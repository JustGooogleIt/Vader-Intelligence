"""Cross-component hardening (release milestone 6): failures that cross boundaries.

Deterministic fault injection on disposable synthetic archives: legacy rows beside new
work, backup/restore preserving interpretation and provenance, limits that bound actual
work, duplicate logical work under concurrency, and interrupted reconstruction.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest
from _f34 import GAME_ID, at, collect, protocol, tick_at, to_schema4
from conftest import Clock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from test_materialization import AS_OF, forward_slice, settle, settlement_evidence  # noqa: E402

from ops import archive as ops_archive  # noqa: E402
from vader_intelligence.evaluation.service import evaluate  # noqa: E402
from vader_intelligence.forecast import journal  # noqa: E402
from vader_intelligence.forecast.reconstruct import reconstruct  # noqa: E402
from vader_intelligence.forecast.references import digest  # noqa: E402
from vader_intelligence.provenance import RunProvenanceV1  # noqa: E402
from vader_intelligence.research import report as reports  # noqa: E402
from vader_intelligence.storage import Store, writer_lock  # noqa: E402

PROTO = protocol()
NS = PROTO.observer_namespace
GAME_DAY = date(2026, 9, 27)


def legacy_rows(store):
    """Contract-1 receipt and a reconstruction pass written through the journal API on
    schema 3, preserved through the explicit migration: valid, inspectable, never proof."""
    db = store.db
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "legacy-import",
            "forecast-storage",
            at(10, 0).isoformat(),
            at(10, 1).isoformat(),
            "complete",
            RunProvenanceV1("legacy-import", "forecast-storage", "a" * 64, {}).to_json(),
            "{}",
        ),
    )
    code_hash = "a" * 64
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "legacy-collect",
            "collect",
            at(10, 0).isoformat(),
            at(10, 1).isoformat(),
            "complete",
            RunProvenanceV1("legacy-collect", "collect", code_hash, {"legacy": True}).to_json(),
            "{}",
        ),
    )
    pass_payload = {
        "schema_version": 1,
        "run_id": "legacy-import",
        "created_at": at(10, 1).isoformat(),
        "code_hash": code_hash,
        "config_hash": digest({}),
        "mode": "historical_reconstruction",
        "references": [],
        "session_id": "legacy-reconstruction",
        "phase_sequence": 1,
        "completed_at": at(10, 1).isoformat(),
        "complete": True,
        "reasons": [],
        "counts": {"games": 0, "contracts": 0},
        "eligibility_after_id": 0,
        "eligibility_through_id": 0,
        "manifest": {"legacy": True},
    }
    return journal.append(db, "discovery_passes", pass_payload)


def test_legacy_records_never_block_unrelated_valid_work(store, config, monkeypatch):
    from vader_intelligence.forecast.schema import migrate as forecast_migrate
    from vader_intelligence.settlement.schema import migrate as settlement_migrate

    settlement_migrate(store)
    forecast_migrate(store.db)  # schema 3
    legacy_pass = legacy_rows(store)
    forecast_migrate(store.db, record_contract=3)  # schema 4, rows preserved
    collect(store, config, origin=at(18, 3))
    warm = tick_at(store, PROTO, at(18, 3, 30))
    assert warm["witness"]["pre"]["skipped_legacy_subjects"] == 1 and warm["errors"] == []
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_eligible"] == 1 and due["publications"]["allowed"] == 1
    settle(store, config, monkeypatch, settlement_evidence())
    clock = Clock()
    clock.origin = at(23, 40)
    result = evaluate(
        store,
        PROTO,
        view="forward-shadow",
        outcomes_as_of=AS_OF,
        clock=clock.now,
        monotonic=clock.mono,
    )
    assert result["status"] == "complete" and result["report"]["population"]["scored_paired"] == 1
    integrity = journal.integrity(store.db)
    assert integrity["record_contracts"]["discovery_passes"]["1"] >= 2  # legacy pass counted
    payload = json.loads(
        store.db.execute(
            "SELECT payload_json FROM discovery_passes WHERE id=?", (legacy_pass,)
        ).fetchone()[0]
    )
    assert payload["mode"] == "historical_reconstruction"  # original meaning preserved
    assert store.verify_integrity() == []


def test_backup_and_restore_preserve_interpretation_and_provenance(
    store, config, monkeypatch, tmp_path
):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    clock = Clock()
    clock.origin = at(23, 40)
    result = evaluate(
        store,
        PROTO,
        view="forward-shadow",
        outcomes_as_of=AS_OF,
        clock=clock.now,
        monotonic=clock.mono,
    )
    evaluation_id = result["evaluation_id"]
    before_report = reports.machine_report(store.db, evaluation_id, include_items=True)
    before_integrity = journal.integrity(store.db)
    before_decisions = store.db.execute(
        "SELECT id, digest, payload_json FROM forecast_decisions ORDER BY id"
    ).fetchall()
    store.close()
    source = Path(config.database)
    backup, restored = tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3"
    assert ops_archive.backup(source, backup)["schema_version"] == 5
    assert ops_archive.backup(backup, restored)["schema_version"] == 5
    reopened = Store(restored, min_free_bytes=1, readonly=True)
    try:
        assert (
            reports.machine_report(reopened.db, evaluation_id, include_items=True) == before_report
        )
        assert journal.integrity(reopened.db) == before_integrity
        assert (
            reopened.db.execute(
                "SELECT id, digest, payload_json FROM forecast_decisions ORDER BY id"
            ).fetchall()
            == before_decisions
        )
        assert reopened.verify_integrity() == []
        inspected = ops_archive.inspect(restored)
        assert inspected["research"]["record_contracts"]["evaluation_runs"] == {
            "1": 1,
            "2": 0,
            "3": 0,
            "4": 0,
        }
    finally:
        reopened.close()
    # A fresh Store on the restored copy re-evaluates to the existing evaluation: same key.
    with writer_lock(restored):
        writable = Store(restored, min_free_bytes=1)
        try:
            again = evaluate(
                writable,
                PROTO,
                view="forward-shadow",
                outcomes_as_of=AS_OF,
                clock=clock.now,
                monotonic=clock.mono,
            )
            assert again["existing"] is True and again["evaluation_id"] == evaluation_id
        finally:
            writable.close()


def test_population_limit_bounds_actual_work(store, config):
    to_schema4(store)
    db = store.db
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "bulk-tick",
            "forecast-tick",
            at(1, 0).isoformat(),
            None,
            "running",
            RunProvenanceV1(
                "bulk-tick",
                "forecast-tick",
                "a" * 64,
                {},
                tool_configuration={"source_lineage": "synthetic"},
            ).to_json(),
            None,
        ),
    )
    for n in range(1001):
        journal.append(
            db,
            "forecast_bindings",
            {
                "schema_version": 1,
                "run_id": "bulk-tick",
                "created_at": at(1, 0).isoformat(),
                "code_hash": "a" * 64,
                "config_hash": PROTO.config_hash,
                "mode": "synthetic",
                "references": [],
                "candidate_key": str(800000 + n),
                "revision": 1,
                "previous_id": None,
                "policy_version": PROTO.policy_version,
                "game_id": 800000 + n,
                "event_ticker": f"KXMLBGAME-BULK{n}",
                "ticker": f"KXMLBGAME-BULK{n}-A",
                "yes_team_id": 100,
                "team_ids": [100, 101],
                "original_date": "2026-09-27",
                "original_start": at(19, 5).isoformat(),
                "game_number": None,
                "scheduled_start": at(19, 5).isoformat(),
                "cutoff": at(18, 5).isoformat(),
                "status": "prepared",
                "reasons": [],
                "manifest": {"bulk": True},
                "terms_digest": "b" * 64,
            },
        )
    clock = Clock()
    clock.origin = at(23, 40)
    with pytest.raises(ValueError, match="above the 1000 limit"):
        evaluate(
            store,
            PROTO,
            view="forward-shadow",
            outcomes_as_of=AS_OF,
            clock=clock.now,
            monotonic=clock.mono,
        )
    assert store.db.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 0
    with pytest.raises(ValueError, match="more than 1000 games"):
        reconstruct(
            store,
            PROTO,
            dataset="bulk-study",
            start_date=GAME_DAY,
            through_date=GAME_DAY,
            clock=clock.now,
            monotonic=clock.mono,
        )


def test_concurrent_evaluations_produce_one_evaluation(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    store.close()
    path = config.database

    def run_evaluate(_):
        clock = Clock()
        clock.origin = at(23, 40)
        try:
            with writer_lock(path):
                s = Store(path, min_free_bytes=1)
                try:
                    result = evaluate(
                        s,
                        PROTO,
                        view="forward-shadow",
                        outcomes_as_of=AS_OF,
                        clock=clock.now,
                        monotonic=clock.mono,
                    )
                    return (result["status"], result["existing"], result["evaluation_id"])
                finally:
                    s.close()
        except RuntimeError:
            return ("busy", None, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run_evaluate, range(2)))
    reopened = Store(path, min_free_bytes=1, readonly=True)
    try:
        assert reopened.db.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 1
    finally:
        reopened.close()
    assert any(o[0] == "complete" and o[1] is False for o in outcomes)
    assert all(o[0] in ("complete", "busy") for o in outcomes)


def test_interrupted_reconstruction_resumes_without_duplicates(store, config, monkeypatch):
    forward_slice(store, config)
    # A second forward binding/decision for another day gives the reconstruction two games.
    second_day = GAME_DAY + timedelta(days=1)
    from vader_intelligence.forecast import service as service_module

    real = service_module.decide_opportunity
    calls = {"n": 0}

    def crash_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt()
        return real(*args, **kwargs)

    # Add a binding for a second game (no decision) in the forward cohort so reconstruct
    # has two opportunities: the first succeeds, the second is interrupted.
    first_binding = json.loads(
        store.db.execute("SELECT payload_json FROM forecast_bindings").fetchone()[0]
    )
    run_id = first_binding["run_id"]
    binding = dict(first_binding)
    binding.update(
        candidate_key="824949",
        game_id=824949,
        event_ticker="KXMLBGAME-26SEP281505HOUATH",
        ticker="KXMLBGAME-26SEP281505HOUATH-HOU",
        original_date=second_day.isoformat(),
        original_start=(at(19, 5) + timedelta(days=1)).isoformat(),
        scheduled_start=(at(19, 5) + timedelta(days=1)).isoformat(),
        cutoff=(at(18, 5) + timedelta(days=1)).isoformat(),
        references=[],
        run_id=run_id,
    )
    journal.append(store.db, "forecast_bindings", binding)
    monkeypatch.setattr(service_module, "decide_opportunity", crash_second)
    clock = Clock()
    clock.origin = at(23, 40) + timedelta(days=1)
    with pytest.raises(KeyboardInterrupt):
        reconstruct(
            store,
            PROTO,
            dataset="study-x",
            start_date=GAME_DAY,
            through_date=second_day,
            clock=clock.now,
            monotonic=clock.mono,
        )
    assert (
        store.db.execute("SELECT status FROM runs WHERE kind='forecast-reconstruct'").fetchone()[0]
        == "interrupted"
    )
    written = store.db.execute(
        "SELECT COUNT(*) FROM forecast_decisions WHERE json_extract(payload_json,'$.dataset')='study-x'"
    ).fetchone()[0]
    assert written == 1  # the first decision is durable, the second never half-written
    monkeypatch.setattr(service_module, "decide_opportunity", real)
    resumed = reconstruct(
        store,
        PROTO,
        dataset="study-x",
        start_date=GAME_DAY,
        through_date=second_day,
        clock=clock.now,
        monotonic=clock.mono,
    )
    assert resumed["decisions_existing"] == 1 and resumed["bindings_in_range"] == 2
    assert resumed["decisions_eligible"] + resumed["decisions_abstained"] == 1
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM forecast_decisions WHERE json_extract(payload_json,'$.dataset')='study-x'"
        ).fetchone()[0]
        == 2
    )
    assert store.verify_integrity() == []


def test_publication_never_precedes_its_cutoff(store, config):
    """A tick before C prepares only: no decision, no publication, whatever the clock."""
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    early = tick_at(store, PROTO, at(18, 4, 59))
    assert early["due_opportunities"] == 0 and early["publications_pending_before"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM forecast_publications").fetchone()[0] == 0
    boundary = tick_at(store, PROTO, at(18, 5, 0))  # C exactly: due
    assert boundary["due_opportunities"] == 1 and boundary["decisions_eligible"] == 1
    attempted, cutoff = store.db.execute(
        "SELECT json_extract(p.payload_json,'$.attempted_at'), json_extract(d.payload_json,'$.cutoff')"
        " FROM forecast_publications p JOIN forecast_decisions d ON d.id=p.decision_id"
    ).fetchone()
    assert attempted >= cutoff


def test_game_id_in_report_is_the_frozen_one(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    clock = Clock()
    clock.origin = at(23, 40)
    result = evaluate(
        store,
        PROTO,
        view="forward-shadow",
        outcomes_as_of=AS_OF,
        clock=clock.now,
        monotonic=clock.mono,
    )
    assert list(result["report"]["annotations"]) == [f"game:{GAME_ID}"]
