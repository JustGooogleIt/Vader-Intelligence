"""Synthetic reproductions of the independently reported PR #7 P2 defects."""

import copy
import json
import sqlite3

import pytest

from ops import archive
from vader_intelligence.forecast import contracts, journal, schema
from vader_intelligence.forecast.references import digest
from vader_intelligence.provenance import RunProvenanceV1, json_text

from .fixtures import AT, HASH, legacy, populated, snapshot


@pytest.fixture
def sample(tmp_path):
    db, payloads = populated(tmp_path / "synthetic.sqlite3")
    # Deliberately simulate a non-fixture writer; all test inputs remain synthetic.
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "other-run",
            "collect",
            AT,
            AT,
            "complete",
            RunProvenanceV1("other-run", "collect", HASH, {}).to_json(),
            "{}",
        ),
    )
    try:
        yield db, payloads
    finally:
        db.close()


def decision(payload, *, dataset, mode, supersedes_id):
    p = copy.deepcopy(payload)
    p.update(
        dataset=dataset,
        mode=mode,
        supersedes_id=supersedes_id,
        run_id="synthetic-run" if mode == "synthetic" else "other-run",
        references=[],
        manifest={},
        binding_id=None,
        discovery_id=None,
        cutoff_eligible=False,
        reasons=["synthetic_test_missing_inputs"],
        results={
            name: {"probability": None, "reasons": ["missing"], "inputs": {}}
            for name in ("constant-v1", "midpoint-v1")
        },
    )
    return p


def test_explicit_item_rowid_cannot_replace_history(sample):
    db, _ = sample
    db.execute("PRAGMA recursive_triggers=OFF")
    before = snapshot(db)
    cursor = db.execute("SELECT * FROM evaluation_items")
    row = dict(zip((c[0] for c in cursor.description), cursor.fetchone()))
    row["rowid"] = 1  # First physical row on the vulnerable pre-fix layout.
    p = json.loads(row["payload_json"])
    p.update(opportunity_key="different-opportunity", game_id=900002)
    row.update(
        opportunity_key=p["opportunity_key"],
        game_id=p["game_id"],
        payload_json=json_text(p),
        digest=digest(p),
    )
    with pytest.raises(sqlite3.DatabaseError, match="immutable|rowid"):
        db.execute(
            f"INSERT OR REPLACE INTO evaluation_items({','.join(row)}) "
            f"VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
    assert snapshot(db) == before
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_synthetic_decision_cannot_be_promoted_by_correction(sample):
    db, p = sample
    correction = decision(
        p["forecast_decisions"],
        dataset="correction",
        mode="historical_reconstruction",
        supersedes_id=1,
    )
    before = snapshot(db)
    with pytest.raises(ValueError, match="synthetic.*lineage"):
        journal.append(db, "forecast_decisions", correction)
    assert snapshot(db) == before


@pytest.mark.parametrize("recursive", [0, 1])
@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid"])
@pytest.mark.parametrize("table", list(schema.COLUMNS))
def test_all_physical_identity_aliases_preserve_history(sample, recursive, alias, table):
    db, payloads = sample
    # Give a replacement publication a valid, different decision FK, ensuring
    # rejection comes from physical identity rather than its logical unique key.
    other_decision = journal.append(
        db, "forecast_decisions", payloads["forecast_decisions"] | {"dataset": "other"}
    )
    db.execute(f"PRAGMA recursive_triggers={recursive}")
    before = snapshot(db)
    cursor = db.execute(f"SELECT * FROM {table} LIMIT 1")
    row = dict(zip((c[0] for c in cursor.description), cursor.fetchone()))
    row.pop("id", None)
    row[alias] = 1
    if "idempotency_key" in row:
        row["idempotency_key"] = "f" * 64
    changes = {
        "discovery_passes": {"session_id": "other"},
        "forecast_bindings": {"candidate_key": "other"},
        "forecast_publications": {"decision_id": other_decision},
        "evaluation_items": {"opportunity_key": "other", "game_id": 900002},
    }.get(table, {})
    p = json.loads(row["payload_json"]) | changes
    row.update(changes, payload_json=json_text(p), digest=digest(p))
    for statement in ("INSERT OR REPLACE", "REPLACE"):
        with pytest.raises(sqlite3.DatabaseError, match="immutable|rowid|oid"):
            db.execute(
                f"{statement} INTO {table}({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        assert snapshot(db) == before
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    # Exact application retries still return the original row/report.
    if table.startswith("evaluation_"):
        assert (
            journal.append_evaluation(
                db, payloads["evaluation_runs"], [payloads["evaluation_items"]]
            )
            == 1
        )
    else:
        assert journal.append(db, table, payloads[table]) == 1
    assert journal.integrity(db)["integrity"] == "verified"


def lineage_payload(payloads, table, mode, sequence, supersedes_id):
    if table == "forecast_decisions":
        return decision(
            payloads[table], dataset=f"lineage-{sequence}", mode=mode, supersedes_id=supersedes_id
        )
    p = copy.deepcopy(payloads[table])
    p.update(
        dataset="lineage-study",
        mode=mode,
        run_id="synthetic-run" if mode == "synthetic" else "other-run",
        view="forward-shadow" if mode == "forward_shadow" else "cutoff-reconstruction",
        supersedes_id=supersedes_id,
        correction_reason="synthetic-test" if supersedes_id else None,
        outcomes_as_of=f"2026-09-30T19:00:0{sequence}+00:00",
        references=[],
        population_manifest=[],
        publication_manifest=[],
        outcome_manifest=[],
        publication_digest=digest([]),
        outcome_digest=digest([]),
        report={"items_digest": digest([])},
    )
    return p


def append(db, table, payload):
    return (
        journal.append_evaluation(db, payload, [])
        if table == "evaluation_runs"
        else journal.append(db, table, payload)
    )


def raw_insert(db, table, payload, identifier=None):
    """Deliberately bypass application validation to model an already bad archive."""
    row = {k: payload[k] for k in schema.PROJECTIONS[table]}
    row.update(digest=digest(payload), payload_json=contracts.validate(table, payload))
    if table != "evaluation_items":
        row["idempotency_key"] = contracts.key(table, payload)
    if identifier is not None:
        row["id"] = identifier
    return db.execute(
        f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
        tuple(row.values()),
    ).lastrowid


@pytest.mark.parametrize("table", ["forecast_decisions", "evaluation_runs"])
def test_direct_and_multistep_synthetic_lineage_rejected(sample, table):
    db, payloads = sample
    root = append(db, table, lineage_payload(payloads, table, "synthetic", 0, None))
    invalid = lineage_payload(payloads, table, "historical_reconstruction", 1, root)
    before = snapshot(db)
    with pytest.raises(ValueError, match="synthetic correction lineage"):
        append(db, table, invalid)
    assert snapshot(db) == before
    # Preserve canonical payloads, valid FK/projections and SQL guards. Only the
    # ancestry is invalid, as can happen with records written by the pre-fix API.
    middle = raw_insert(db, table, invalid)
    leaf = lineage_payload(payloads, table, "historical_reconstruction", 2, middle)
    before = snapshot(db)
    with pytest.raises(ValueError, match="synthetic correction lineage"):
        append(db, table, leaf)
    assert snapshot(db) == before
    raw_insert(db, table, leaf)
    # Validate the leaf explicitly: rejecting an earlier row in a full scan alone
    # would not demonstrate transitive protection when persisting a new correction.
    with pytest.raises(ValueError, match="synthetic correction lineage"):
        journal.validate_links(db, table, leaf)
    with pytest.raises(ValueError, match="synthetic correction lineage"):
        journal.integrity(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with pytest.raises(ValueError, match="synthetic correction lineage"):
        archive.inspect(path)


@pytest.mark.parametrize(
    "table,mode",
    [
        ("forecast_decisions", "synthetic"),
        ("forecast_decisions", "historical_reconstruction"),
        ("evaluation_runs", "synthetic"),
        ("evaluation_runs", "historical_reconstruction"),
        ("evaluation_runs", "forward_shadow"),
    ],
)
def test_same_mode_correction_chains_and_retries_supported(sample, table, mode):
    db, payloads = sample
    previous = None
    for sequence in range(3):
        p = lineage_payload(payloads, table, mode, sequence, previous)
        previous = append(db, table, p)
        before = snapshot(db)
        assert append(db, table, p) == previous
        assert snapshot(db) == before
    assert journal.integrity(db)["integrity"] == "verified"


@pytest.mark.parametrize("table", ["forecast_decisions", "evaluation_runs"])
def test_malformed_correction_cycle_fails_closed(sample, table):
    db, payloads = sample
    cyclic = lineage_payload(payloads, table, "synthetic", 0, 99)
    raw_insert(db, table, cyclic, identifier=99)
    with pytest.raises(ValueError, match="cyclic correction lineage"):
        journal.integrity(db)


def old_layout(path, payloads):
    """Recreate the actual pre-fix schema-3 definition with populated research rows."""
    db = legacy(path)
    sql = schema.migration_sql().replace(") WITHOUT ROWID;", ");")
    for statement in schema.statements(sql):
        db.execute(statement)
    db.execute("INSERT INTO schema_migrations VALUES (3,?)", (AT,))
    db.execute("PRAGMA user_version=3")
    for table in schema.COLUMNS:
        p = payloads[table]
        if table == "evaluation_items":
            p = p | {"evaluation_id": 1}
        raw_insert(db, table, p)
    return db


def test_old_schema_three_is_rejected_without_mutation(sample, tmp_path):
    _, p = sample
    path = tmp_path / "pre-fix.sqlite3"
    db = old_layout(path, p)
    try:
        before = snapshot(db)
        ddl = db.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
        for operation in (
            lambda: schema.require_schema(db),
            lambda: schema.check_schema(db),
            lambda: schema.migrate(db),
            lambda: journal.append(db, "discovery_passes", p["discovery_passes"]),
            lambda: journal.append_evaluation(db, p["evaluation_runs"], [p["evaluation_items"]]),
            lambda: journal.integrity(db),
            lambda: archive.inspect(path),
            lambda: archive.backup(path, tmp_path / "must-not-be-created.sqlite3"),
        ):
            with pytest.raises(ValueError, match="unsupported schema-3 layout"):
                operation()
            assert snapshot(db) == before
            assert db.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall() == ddl
            assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not (tmp_path / "must-not-be-created.sqlite3").exists()
    finally:
        db.close()
