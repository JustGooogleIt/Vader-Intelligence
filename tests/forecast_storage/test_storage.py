import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ops import archive
from vader_intelligence.forecast import journal, schema
from vader_intelligence.forecast.references import digest, reference

from .fixtures import connect, envelopes, legacy, populated, snapshot


@pytest.fixture
def sample(tmp_path):
    db, p = populated(tmp_path / "synthetic.sqlite3")
    try:
        yield db, p
    finally:
        db.close()


def test_populated_legacy_preserved_and_repeat_migration(tmp_path):
    db = legacy(tmp_path / "old.sqlite3")
    before = snapshot(db)
    assert schema.migrate(db) is True
    after = snapshot(db)
    for table in before:
        assert before[table] == (after[table][:2] if table == "schema_migrations" else after[table])
    assert len(after["settlement_versions"]) == 3  # Synthetic A→B→A stays three revisions.
    assert schema.migrate(db) is False
    assert snapshot(db) == after
    db.close()


def test_schema_one_requires_separate_upgrade(tmp_path):
    db = legacy(tmp_path / "one.sqlite3", 1)
    before = snapshot(db)
    with pytest.raises(ValueError, match="settlement migrate first"):
        schema.migrate(db)
    assert snapshot(db) == before
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    db.close()


def test_migration_failure_rolls_back_every_ddl_statement(tmp_path, monkeypatch):
    db = legacy(tmp_path / "rollback.sqlite3")
    before = snapshot(db)
    ddl = db.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
    script = schema.migration_sql()
    monkeypatch.setattr(
        schema,
        "migration_sql",
        lambda: script + "\nINSERT INTO table_that_does_not_exist VALUES (1);\n",
    )
    with pytest.raises(sqlite3.Error):
        schema.migrate(db)
    assert snapshot(db) == before
    assert db.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall() == ddl
    assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    db.close()


@pytest.mark.parametrize("version", [0, -1, 4, 999])
def test_future_and_unsupported_versions_fail_without_changes(tmp_path, version):
    path = tmp_path / "future.sqlite3"
    db = legacy(path)
    db.execute(f"PRAGMA user_version={version}")
    before = snapshot(db)
    with pytest.raises(ValueError):
        schema.migrate(db)
    with pytest.raises(ValueError):
        archive.inspect(path)
    assert snapshot(db) == before
    assert db.execute("PRAGMA user_version").fetchone()[0] == version
    db.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_all_tables_survive_sqlite_backup_restore(tmp_path, version):
    path = tmp_path / "source.sqlite3"
    db = populated(path)[0] if version == 3 else legacy(path, version)
    before = snapshot(db)
    report = archive.backup(path, tmp_path / "backup.sqlite3")
    assert report["schema_version"] == version
    archive.backup(tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3")
    restored = connect(tmp_path / "restored.sqlite3")
    assert snapshot(restored) == before
    if version == 3:
        assert set(schema.COLUMNS) <= set(report["counts"])
        assert journal.integrity(restored)["materialization"] == "not_implemented"
    restored.close()
    db.close()


@pytest.mark.parametrize("table", list(schema.COLUMNS)[:-2])
def test_exact_retry_and_conflicting_content(sample, table):
    db, p = sample
    before = snapshot(db)
    assert journal.append(db, table, p[table]) == 1
    changed = copy.deepcopy(p[table])
    changed["created_at"] = "2026-09-30T19:00:01+00:00"
    with pytest.raises(journal.IdempotencyConflict):
        journal.append(db, table, changed)
    assert snapshot(db) == before


def test_evaluation_retry_and_atomic_failed_item(sample):
    db, p = sample
    before = snapshot(db)
    assert journal.append_evaluation(db, p["evaluation_runs"], [p["evaluation_items"]]) == 1
    changed = copy.deepcopy(p["evaluation_runs"])
    changed["outcomes_as_of"] = "2026-09-30T19:00:01+00:00"
    item = copy.deepcopy(p["evaluation_items"])
    del item["scores"]
    changed["report"]["items_digest"] = digest([item])
    with pytest.raises(ValueError, match="incomplete"):
        journal.append_evaluation(db, changed, [item])
    assert snapshot(db) == before


@pytest.mark.parametrize("table", list(schema.COLUMNS))
def test_sql_append_only_and_replace_rejected(sample, table):
    db, _ = sample
    before = snapshot(db)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(f"UPDATE {table} SET digest=digest")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
    assert snapshot(db) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding_id", 999),
        ("discovery_id", 999),
        ("game_id", 999),
        ("cutoff_eligible", False),
        ("results", {}),
        ("baseline_versions", ["constant-v1"]),
        ("source_ceiling", -1),
    ],
)
def test_invalid_and_incomplete_decisions(sample, field, value):
    db, p = sample
    candidate = copy.deepcopy(p["forecast_decisions"])
    candidate["dataset"] = "negative-test"
    candidate[field] = value
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        journal.append(db, "forecast_decisions", candidate)


def test_invalid_reference_kind_and_retrieval_provenance(sample):
    db, p = sample
    candidate = copy.deepcopy(p["discovery_passes"])
    candidate["session_id"] = "other-pass"
    for key, value in [
        ("digest", "0" * 64),
        ("kind", "invented"),
        ("parser_version", 999),
        ("fetches", []),
    ]:
        ref = reference(db, "entities", {"id": 1})
        ref[key] = value
        candidate["references"] = [ref]
        with pytest.raises(ValueError, match="provenance"):
            journal.append(db, "discovery_passes", candidate)


def test_nested_refs_checked_and_corrupt_archive_detected(sample):
    db, p = sample
    candidate = copy.deepcopy(p["discovery_passes"])
    candidate["session_id"] = "other-pass"
    candidate["manifest"]["nested"] = reference(db, "fetches", {"id": "fetch-2"})
    candidate["manifest"]["nested"]["fetches"][0]["seq"] = 99
    with pytest.raises(ValueError, match="provenance"):
        journal.append(db, "discovery_passes", candidate)
    db.execute("UPDATE blobs SET body=x'00'")
    with pytest.raises(ValueError, match="raw bytes"):
        journal.integrity(db)


def test_receipt_exactly_one_subject_and_no_fabricated_digest(sample):
    db, p = sample
    for changes in (
        {"binding_id": 1},
        {"subject_digest": "b" * 64},
        {"subject_kind": "fetch"},
        {"publication_id": None},
    ):
        candidate = p["forecast_receipts"] | changes
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            journal.append(db, "forecast_receipts", candidate)


def test_synthetic_lineage_cannot_be_promoted(sample):
    db, p = sample
    for table in list(schema.COLUMNS)[:-2]:
        with pytest.raises(ValueError):
            journal.append(db, table, p[table] | {"mode": "forward_shadow"})


def test_binding_unchanged_and_aba_revisions(sample):
    db, p = sample
    initial = p["forecast_bindings"]
    assert journal.append(db, "forecast_bindings", initial | {"previous_id": 1, "revision": 2}) == 1
    changed = initial | {"previous_id": 1, "revision": 2, "status": "synthetic-B"}
    assert journal.append(db, "forecast_bindings", changed) == 2
    assert journal.append(db, "forecast_bindings", initial | {"previous_id": 2, "revision": 3}) == 3
    assert db.execute(
        "SELECT revision,previous_id FROM forecast_bindings ORDER BY id"
    ).fetchall() == [(1, None), (2, 1), (3, 2)]


def test_one_publication_cannot_be_replaced_by_veto(sample):
    db, p = sample
    with pytest.raises(journal.IdempotencyConflict):
        journal.append(
            db,
            "forecast_publications",
            p["forecast_publications"] | {"verdict": "vetoed", "reasons": ["later_warning"]},
        )
    assert db.execute("SELECT verdict FROM forecast_publications").fetchone()[0] == "allowed"


def test_two_sqlite_writers_same_key_serialize(tmp_path):
    path = tmp_path / "race.sqlite3"
    db = legacy(path)
    schema.migrate(db)
    p = envelopes(db)["discovery_passes"] | {"session_id": "concurrent-session"}
    db.close()

    def insert(_):
        connection = connect(path)
        try:
            return journal.append(connection, "discovery_passes", p)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(insert, range(2)))
    assert results == [2, 2]  # Real SQLite serialization; no claim of flock verification.


def test_no_nested_commit_or_missing_fk_enforcement(sample):
    db, p = sample
    db.execute("BEGIN")
    with pytest.raises(ValueError, match="idle connection"):
        schema.migrate(db)
    assert db.in_transaction
    db.execute("ROLLBACK")
    db.execute("PRAGMA foreign_keys=OFF")
    with pytest.raises(ValueError, match="foreign_keys"):
        journal.append(db, "discovery_passes", p["discovery_passes"])


def test_integrity_is_read_only(sample):
    db, _ = sample
    before = snapshot(db)
    assert journal.integrity(db) == {
        "integrity": "verified",
        "materialization": "not_implemented",
        "publication_verified": False,
    }
    assert snapshot(db) == before


def test_evaluation_duplicate_game_and_incomplete_population_roll_back(sample):
    db, p = sample
    before = snapshot(db)
    report = copy.deepcopy(p["evaluation_runs"])
    report["outcomes_as_of"] = "2026-09-30T19:00:01+00:00"
    first = p["evaluation_items"]
    second = first | {"opportunity_key": "duplicate-same-game"}
    items = [first, second]
    report["population_manifest"] = [
        {k: i[k] for k in ("opportunity_key", "decision_id", "game_id", "horizon")} for i in items
    ]
    report["report"]["items_digest"] = digest(items)
    with pytest.raises(sqlite3.IntegrityError):
        journal.append_evaluation(db, report, items)
    assert snapshot(db) == before
    with pytest.raises(ValueError, match="incomplete evaluation"):
        journal.append_evaluation(db, report, [first])
    assert snapshot(db) == before


def test_evaluation_corrections_append_without_rewriting_cohort(sample):
    db, p = sample
    original = snapshot(db)
    previous = 1
    for n, version in enumerate((2, 1), 1):
        report = copy.deepcopy(p["evaluation_runs"])
        report["supersedes_id"] = previous
        report["correction_reason"] = "synthetic-new-observation"
        report["outcomes_as_of"] = f"2026-09-30T19:00:0{n}+00:00"
        report["outcome_manifest"] = [reference(db, "settlement_versions", {"id": version})]
        report["outcome_digest"] = digest(report["outcome_manifest"])
        item = copy.deepcopy(p["evaluation_items"])
        item["kalshi_id"] = version
        item["references"] = [reference(db, "settlement_versions", {"id": version})]
        report["report"]["items_digest"] = digest([item])
        previous = journal.append_evaluation(db, report, [item])
    assert db.execute("SELECT supersedes_id FROM evaluation_runs ORDER BY id").fetchall() == [
        (None,),
        (1,),
        (2,),
    ]
    assert snapshot(db)["forecast_decisions"] == original["forecast_decisions"]
    assert snapshot(db)["evaluation_runs"][0] == original["evaluation_runs"][0]
    assert journal.integrity(db)["integrity"] == "verified"


@pytest.mark.parametrize(
    "changes",
    [
        {"y": True},
        {"y": 1, "payout": "0.5300"},
        {"y": 1, "payout": "1.0000"},
        {"mlb_id": 3},
        {"references": []},
    ],
)
def test_ineligible_or_wrong_kind_outcome_evidence_rejected(sample, changes):
    db, p = sample
    item = p["evaluation_items"] | changes
    report = copy.deepcopy(p["evaluation_runs"])
    report["outcomes_as_of"] = "2026-09-30T19:00:01+00:00"
    report["report"]["items_digest"] = digest([item])
    with pytest.raises(ValueError):
        journal.append_evaluation(db, report, [item])


def test_nested_json_foreign_reference_corruption_detected_on_backup(sample, tmp_path):
    db, p = sample
    db.execute("DROP TRIGGER discovery_passes_no_update")
    bad = copy.deepcopy(p["discovery_passes"])
    bad["references"][0]["key"]["id"] = "missing-fetch"
    # Simulate disk/operator corruption; even recomputing its digest cannot hide a missing FK.
    from vader_intelligence.provenance import json_text

    db.execute(
        "UPDATE discovery_passes SET payload_json=?,digest=? WHERE id=1",
        (json_text(bad), digest(bad)),
    )
    source = tmp_path / "corrupted.sqlite3"
    dst = connect(source)
    db.backup(dst)
    dst.close()
    with pytest.raises(ValueError, match="immutability"):
        archive.inspect(source)
    # Restore trigger only, then semantic JSON-reference validation must still fail.
    db.execute(
        "CREATE TRIGGER discovery_passes_no_update BEFORE UPDATE ON discovery_passes BEGIN SELECT RAISE(ABORT,'immutable'); END"
    )
    with pytest.raises(ValueError, match="missing reference"):
        journal.integrity(db)


def test_sql_constraints_and_foreign_keys_reject_bypass(sample):
    db, _ = sample
    row = list(db.execute("SELECT * FROM forecast_publications").fetchone())
    columns = [c[1] for c in db.execute("PRAGMA table_info(forecast_publications)")]
    row[columns.index("id")] = 99
    row[columns.index("idempotency_key")] = "f" * 64
    row[columns.index("decision_id")] = 999
    payload = json.loads(row[columns.index("payload_json")])
    payload["decision_id"] = 999
    from vader_intelligence.provenance import json_text

    row[columns.index("payload_json")] = json_text(payload)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "INSERT INTO forecast_publications VALUES (" + ",".join("?" for _ in row) + ")", row
        )


def test_transaction_failure_rolls_back_record(sample):
    db, p = sample
    before = snapshot(db)
    with pytest.raises(RuntimeError, match="injected"):
        with schema.transaction(db):
            journal._insert(
                db, "discovery_passes", p["discovery_passes"] | {"session_id": "interrupted"}
            )
            raise RuntimeError("injected after insert before commit")
    assert snapshot(db) == before


def test_identical_duplicate_items_cannot_leave_incomplete_report(sample):
    db, p = sample
    before = snapshot(db)
    with pytest.raises(ValueError, match="duplicate evaluation opportunity"):
        journal.append_evaluation(db, p["evaluation_runs"], [p["evaluation_items"]] * 2)
    assert snapshot(db) == before


def test_code_identity_and_receipt_clock_contradictions(sample):
    db, p = sample
    with pytest.raises(ValueError, match="code identity"):
        journal.append(db, "discovery_passes", p["discovery_passes"] | {"code_hash": "b" * 64})
    with pytest.raises(ValueError, match="predates"):
        journal.append(
            db,
            "forecast_receipts",
            p["forecast_receipts"] | {"observed_at": "2026-09-30T18:59:59+00:00"},
        )


def test_timely_publication_claim_requires_its_receipt(sample):
    db, p = sample
    item = p["evaluation_items"] | {"publication_status": "published_timely", "receipt_id": None}
    report = copy.deepcopy(p["evaluation_runs"])
    report["outcomes_as_of"] = "2026-09-30T19:00:01+00:00"
    report["report"]["items_digest"] = digest([item])
    with pytest.raises(ValueError, match="requires decision, publication and receipt"):
        journal.append_evaluation(db, report, [item])
