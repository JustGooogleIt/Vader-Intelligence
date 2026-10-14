"""Record contract 2 and schema 4 (F3.3 plan §9.6, §9.7): cases I4, I5, J2–J4, K, N1–N12.

Every archive here is synthetic and disposable. Contract-1 rows written by the shipped
fixtures model records that an operator may already have persisted through the journal
API; they must stay valid, inspectable and never become evidence.
"""

import copy
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from ops import archive
from vader_intelligence.forecast import contracts, journal, schema
from vader_intelligence.forecast.references import digest, reference
from vader_intelligence.provenance import RunProvenanceV1, json_text

from .fixtures import AT, HASH, connect, envelopes, legacy, populated, snapshot

LIVE_RUN = "live-collect-run"
LIVE_CONFIG = {"max_pages": 3}
# Contract-2 receipts are admissible only from operational witness runs that name the
# receipt's observer namespace and session (plan §4.1). Two such runs: a synthetic one
# (configuration {} so digest({}) stays the fixture's config hash) and a live one.
WITNESS_RUN = "synthetic-witness-run"
LIVE_WITNESS = "live-witness-run"
WITNESS_TOOL = {"observer_namespace": "local-witness-v1", "session_id": "witness-session"}


def witness_run(db, run_id, configuration, lineage):
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            run_id,
            "forecast-witness",
            AT,
            AT,
            "complete",
            RunProvenanceV1(
                run_id,
                "forecast-witness",
                HASH,
                configuration,
                tool_configuration=WITNESS_TOOL | {"source_lineage": lineage},
            ).to_json(),
            "{}",
        ),
    )


@pytest.fixture
def archive3(tmp_path):
    """Schema 3 with every contract-1 fixture row plus a contract-1 *fetch* receipt (N1)."""
    db, payloads = populated(tmp_path / "contract.sqlite3")
    body_hash = db.execute("SELECT body_sha256 FROM fetches WHERE id='fetch-1'").fetchone()[0]
    legacy_fetch_receipt = payloads["forecast_receipts"] | dict(
        subject_kind="fetch",
        fetch_id="fetch-1",
        publication_id=None,
        subject_digest=body_hash,
        session_id="api-caller",
        observer_namespace="api-caller",
    )
    payloads["legacy_fetch_receipt"] = legacy_fetch_receipt
    assert journal.append(db, "forecast_receipts", legacy_fetch_receipt) == 2
    # A live (non-fixture) run so forward_shadow records can exist (K cases).
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            LIVE_RUN,
            "collect",
            AT,
            AT,
            "complete",
            RunProvenanceV1(
                LIVE_RUN,
                "collect",
                HASH,
                LIVE_CONFIG,
                tool_configuration={"collection_protocol": "two-phase-v1"},
            ).to_json(),
            "{}",
        ),
    )
    witness_run(db, WITNESS_RUN, {}, "synthetic")
    witness_run(db, LIVE_WITNESS, LIVE_CONFIG, "live")
    try:
        yield db, payloads
    finally:
        db.close()


def migrate4(db):
    assert schema.migrate(db, record_contract=2) is True
    assert db.execute("PRAGMA user_version").fetchone()[0] == 4


def fetch_receipt(db, p, fetch_id, **changes):
    """A contract-2 receipt for a fetch in the synthetic observer namespace."""
    ref = reference(db, "fetches", {"id": fetch_id})
    return (
        p["forecast_receipts"]
        | dict(
            contract_version=2,
            run_id=WITNESS_RUN,
            subject_kind="fetch",
            fetch_id=fetch_id,
            publication_id=None,
            subject_digest=ref["digest"],
            references=[ref],
            session_id="witness-session",
            observer_namespace="local-witness-v1",
            config_hash=digest({}),  # the witness run's recorded configuration is {}
        )
        | changes
    )


def pass_receipt(db, p, pass_id, **changes):
    ref = reference(db, "discovery_passes", {"id": pass_id})
    return (
        p["forecast_receipts"]
        | dict(
            contract_version=2,
            run_id=WITNESS_RUN,
            subject_kind="discovery_pass",
            discovery_id=pass_id,
            publication_id=None,
            subject_digest=journal.row_dict(db, "discovery_passes", {"id": pass_id})["digest"],
            references=[ref],
            session_id="witness-session",
            observer_namespace="local-witness-v1",
            config_hash=digest({}),
        )
        | changes
    )


def pinned_decision(db, p, pass_id, **changes):
    row = journal.row_dict(db, "discovery_passes", {"id": pass_id})
    pin = {
        "id": pass_id,
        "digest": row["digest"],
        "collector_config_hash": json.loads(row["payload_json"])["config_hash"],
    }
    decision = copy.deepcopy(p["forecast_decisions"]) | dict(
        contract_version=2, dataset="contract-2-study", discovery_id=pass_id
    )
    decision["manifest"] = {"discovery": pin}
    decision.update(changes)
    if decision["game_id"] != p["forecast_decisions"]["game_id"]:
        # Another game has no fixture binding: record an honest abstention instead.
        decision.update(
            binding_id=None,
            ticker=None,
            yes_team_id=None,
            cutoff_eligible=False,
            reasons=["identity_unproven"],
            results={
                name: {"probability": None, "reasons": ["identity_unproven"], "inputs": {}}
                for name in ("constant-v1", "midpoint-v1")
            },
        )
    return decision


def live_pass(db, p, config_hash, session="live-pass"):
    return p["discovery_passes"] | dict(
        run_id=LIVE_RUN,
        mode="forward_shadow",
        references=[],
        session_id=session,
        config_hash=config_hash,
        manifest={"live": True},
    )


def live_binding(db, p, config_hash):
    return p["forecast_bindings"] | dict(
        run_id=LIVE_RUN,
        mode="forward_shadow",
        references=[],
        config_hash=config_hash,
        candidate_key="live-game",
    )


# ------------------------------------------------------------------- N11, N6, N7


def test_explicit_migration_004_touches_no_row(archive3):
    db, _ = archive3
    before = snapshot(db)
    assert schema.migrate(db) is False  # plain migrate at 3 is a no-op
    assert snapshot(db) == before
    migrate4(db)
    after = snapshot(db)
    for table, rows in before.items():
        if table == "schema_migrations":
            assert after[table][:3] == rows and [r[0] for r in after[table]] == [1, 2, 3, 4]
        else:
            assert after[table] == rows  # no row touched, no table changed
    ddl_before = {r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")}
    assert schema.migrate(db, record_contract=2) is False  # no-op at 4
    assert schema.migrate(db) is False  # plain migrate at 4 is a no-op, never a downgrade
    assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert {r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")} == (
        ddl_before
    )
    assert schema.executable(schema.migration_sql("004_record_contract_2.sql")) == ""


def test_record_contract_flag_requires_schema_three(tmp_path):
    db = legacy(tmp_path / "two.sqlite3")
    before = snapshot(db)
    with pytest.raises(ValueError, match="requires schema 3"):
        schema.migrate(db, record_contract=2)
    assert snapshot(db) == before and db.execute("PRAGMA user_version").fetchone()[0] == 2
    with pytest.raises(ValueError, match="unsupported record contract"):
        schema.migrate(db, record_contract=3)
    db.close()


def test_contract_two_rejected_on_schema_three_and_contract_one_closed_on_four(archive3):
    db, p = archive3
    before = snapshot(db)
    with pytest.raises(ValueError, match="record-contract 2"):
        journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-2"))
    assert snapshot(db) == before
    migrate4(db)
    new_contract_one = p["forecast_receipts"] | {"observer_namespace": "another-api-caller"}
    with pytest.raises(ValueError, match="closed"):
        journal.append(db, "forecast_receipts", new_contract_one)
    new_decision = copy.deepcopy(p["forecast_decisions"]) | {"dataset": "late-contract-1"}
    with pytest.raises(ValueError, match="closed"):
        journal.append(db, "forecast_decisions", new_decision)
    # Other tables have one contract and stay open.
    binding = p["forecast_bindings"] | {"previous_id": 1, "revision": 2, "status": "renewed"}
    assert journal.append(db, "forecast_bindings", binding) == 2
    assert journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-2")) == 3


def test_exact_retry_of_contract_one_returns_original_on_both_schemas(archive3):
    db, p = archive3
    before = snapshot(db)
    assert journal.append(db, "forecast_receipts", p["legacy_fetch_receipt"]) == 2
    assert journal.append(db, "forecast_decisions", p["forecast_decisions"]) == 1
    assert snapshot(db) == before
    migrate4(db)
    after = snapshot(db)
    assert journal.append(db, "forecast_receipts", p["legacy_fetch_receipt"]) == 2
    assert journal.append(db, "forecast_decisions", p["forecast_decisions"]) == 1
    assert journal.append(db, "forecast_receipts", p["forecast_receipts"]) == 1
    assert snapshot(db) == after
    # A changed contract-1 envelope is still a conflict, never a replacement.
    with pytest.raises(journal.IdempotencyConflict):
        journal.append(
            db, "forecast_receipts", p["legacy_fetch_receipt"] | {"monotonic_offset": "9"}
        )


# ---------------------------------------------------------------------- N1, N4, N5


def test_legacy_fetch_receipt_stays_valid_under_contract_one_and_counted(archive3):
    db, p = archive3
    row = db.execute("SELECT * FROM forecast_receipts WHERE id=2").fetchone()
    payload = json.loads(
        db.execute("SELECT payload_json FROM forecast_receipts WHERE id=2").fetchone()[0]
    )
    assert "contract_version" not in payload  # contract 1 by absence
    migrate4(db)
    assert db.execute("SELECT * FROM forecast_receipts WHERE id=2").fetchone() == row
    result = journal.integrity(db)
    assert result["integrity"] == "verified"
    assert result["record_contracts"]["forecast_receipts"] == {"1": 2, "2": 0}
    # The legacy receipt's digest is the raw body hash: valid under contract 1 only.
    assert (
        payload["subject_digest"]
        == (db.execute("SELECT body_sha256 FROM fetches WHERE id='fetch-1'").fetchone()[0])
    )


def test_mixed_archive_counts_each_row_under_its_own_contract(archive3):
    db, p = archive3
    migrate4(db)
    journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-2"))
    journal.append(db, "forecast_receipts", pass_receipt(db, p, 1))
    journal.append(db, "forecast_decisions", pinned_decision(db, p, 1))
    result = journal.integrity(db)
    assert result["record_contracts"]["forecast_receipts"] == {"1": 2, "2": 2}
    assert result["record_contracts"]["forecast_decisions"] == {"1": 1, "2": 1}
    assert result["record_contracts"]["discovery_passes"] == {"1": 1, "2": 0}
    # Nothing was converted: the contract-1 rows are byte-identical to their fixtures.
    assert (
        json.loads(
            db.execute("SELECT payload_json FROM forecast_receipts WHERE id=1").fetchone()[0]
        )
        == p["forecast_receipts"]
    )


@pytest.mark.parametrize(
    "table,value",
    [
        ("forecast_receipts", 3),
        ("forecast_receipts", "2"),
        ("forecast_receipts", 1),
        ("forecast_receipts", True),
        ("forecast_decisions", 1),
        ("discovery_passes", 2),
        ("forecast_bindings", 2),
        ("evaluation_runs", 2),
    ],
)
def test_unknown_contract_versions_are_rejected_on_insert(archive3, table, value):
    db, p = archive3
    migrate4(db)
    before = snapshot(db)
    candidate = copy.deepcopy(p[table]) | {"contract_version": value}
    with pytest.raises(ValueError, match="unknown record contract|unknown storage fields"):
        contracts.validate(table, candidate)
    if not table.startswith("evaluation_"):
        with pytest.raises(ValueError):
            journal.append(db, table, candidate)
    assert snapshot(db) == before


def test_unknown_contract_version_in_an_archive_fails_closed_without_mutation(archive3, tmp_path):
    db, p = archive3
    migrate4(db)
    bad = p["forecast_receipts"] | {"contract_version": 3, "session_id": "unknown-version"}
    row = {k: bad[k] for k in schema.PROJECTIONS["forecast_receipts"]}
    row.update(
        digest=digest(bad),
        payload_json=json_text(bad),
        idempotency_key=digest(["forecast_receipts", "unknown", "version"]),
    )
    db.execute(
        f"INSERT INTO forecast_receipts({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
        tuple(row.values()),
    )
    before = snapshot(db)
    with pytest.raises(ValueError, match="forecast_receipts row 3: unknown record contract"):
        journal.integrity(db)
    source = tmp_path / "bad.sqlite3"
    dst = connect(source)
    db.backup(dst)
    dst.close()
    with pytest.raises(ValueError, match="unknown record contract"):
        archive.inspect(source)
    with pytest.raises(ValueError, match="unknown record contract"):
        archive.backup(source, tmp_path / "unverified.sqlite3")
    assert snapshot(db) == before


def test_contract_two_row_in_schema_three_archive_is_an_integrity_failure(archive3):
    db, p = archive3
    migrate4(db)
    journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-2"))
    # Model an archive whose version was lowered by hand: ledger has no triggers.
    db.execute("DELETE FROM schema_migrations WHERE version=4")
    db.execute("PRAGMA user_version=3")
    before = snapshot(db)
    with pytest.raises(ValueError, match="contract-2 record in a schema-3 archive"):
        journal.integrity(db)
    # Neither a plain nor an explicit migration legitimizes the row by advancing the
    # version: both refuse before any change (review finding 3).
    for record_contract in (None, 2):
        with pytest.raises(ValueError, match="contract-2 record in a schema-3 archive"):
            schema.migrate(db, record_contract=record_contract)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert snapshot(db) == before


# ------------------------------------------------------------------------ N8, N10


def test_backup_restore_and_replay_preserve_a_mixed_schema_four_archive(archive3, tmp_path):
    db, p = archive3
    migrate4(db)
    journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-2"))
    journal.append(db, "forecast_decisions", pinned_decision(db, p, 1))
    before = snapshot(db)
    integrity_before = journal.integrity(db)
    report = archive.backup(tmp_path / "contract.sqlite3", tmp_path / "backup.sqlite3")
    assert report["schema_version"] == 4
    assert report["research"]["record_contracts"] == integrity_before["record_contracts"]
    archive.backup(tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3")
    restored = connect(tmp_path / "restored.sqlite3")
    try:
        assert snapshot(restored) == before
        assert restored.execute("PRAGMA user_version").fetchone()[0] == 4
        assert [r[0] for r in restored.execute("SELECT version FROM schema_migrations")] == [
            1,
            2,
            3,
            4,
        ]
        assert journal.integrity(restored) == integrity_before
    finally:
        restored.close()


# ------------------------------------------------------------------------------ N9


def test_binary_at_main_refuses_a_schema_four_archive(archive3, tmp_path):
    """Extract src/ at main (ebb58df) with git archive and run its CLI against schema 4."""
    db, _ = archive3
    migrate4(db)
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    repo = Path(__file__).resolve().parents[2]
    old = tmp_path / "old-binary"
    old.mkdir()
    try:
        bundle = subprocess.run(
            ["git", "-C", str(repo), "archive", "ebb58df9b89922db4828fda7d23891dd917a4b90", "src"],
            capture_output=True,
            timeout=60,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"main at ebb58df unavailable here: {exc}")
    with tarfile.open(fileobj=__import__("io").BytesIO(bundle)) as tar:
        tar.extractall(old, filter="data")
    db.close()
    before = path.read_bytes()
    for command in (["health"], ["db-init"], ["forecast", "migrate"]):
        # PYTHONPATH precedes site-packages, so the extracted main source shadows the
        # worktree's editable install while httpx stays importable.
        result = subprocess.run(
            [sys.executable, "-m", "vader_intelligence.cli", "--db", str(path), *command],
            cwd=old / "src",
            env={"PYTHONPATH": str(old / "src"), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1, (command, result.stdout, result.stderr)
        error = json.loads(result.stdout or result.stderr)["error"]
        assert "unsupported" in error or "schema" in error, (command, error)
    assert path.read_bytes() == before
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 4


# -------------------------------------------------------------------------- I4, I5


def test_contract_two_fetch_receipt_rejects_body_hash_digest_and_fingerprints_rows(archive3):
    db, p = archive3
    migrate4(db)
    body_hash = db.execute("SELECT body_sha256 FROM fetches WHERE id='fetch-1'").fetchone()[0]
    with pytest.raises(ValueError, match="contract-2 receipt subject digest"):
        journal.append(
            db, "forecast_receipts", fetch_receipt(db, p, "fetch-1", subject_digest=body_hash)
        )
    with pytest.raises(ValueError, match="exactly its subject reference"):
        journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-1", references=[]))
    with pytest.raises(ValueError, match="configuration differs"):
        journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-1", config_hash=HASH))
    # I5: fetch-1 and fetch-3 carry byte-identical bodies but are distinct retrievals.
    assert (
        db.execute("SELECT body_sha256 FROM fetches WHERE id='fetch-3'").fetchone()[0] == body_hash
    )
    first = journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-1"))
    second = journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-3"))
    digests = db.execute(
        "SELECT json_extract(payload_json,'$.subject_digest') FROM forecast_receipts WHERE id IN (?,?)",
        (first, second),
    ).fetchall()
    assert digests[0] != digests[1]
    # The contract-1 receipt for fetch-1 and the contract-2 receipt coexist: different keys.
    assert (
        db.execute("SELECT COUNT(*) FROM forecast_receipts WHERE fetch_id='fetch-1'").fetchone()[0]
        == 2
    )
    assert journal.append(db, "forecast_receipts", fetch_receipt(db, p, "fetch-1")) == first


def test_contract_two_receipt_for_a_failed_attempt_without_body(archive3):
    db, p = archive3
    migrate4(db)
    db.execute(
        "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,state,status,error) "
        "VALUES ('fetch-timeout','request-1',2,?,?,'error',NULL,'ReadTimeout')",
        (AT, AT),
    )
    receipt = fetch_receipt(db, p, "fetch-timeout")
    assert receipt["references"][0]["fetches"][0]["raw_sha256"] is None
    assert journal.append(db, "forecast_receipts", receipt) == 3
    db.execute(
        "INSERT INTO fetches(id,request_id,attempt,started_at,state) "
        "VALUES ('fetch-pending','request-1',3,?,'pending')",
        (AT,),
    )
    with pytest.raises(ValueError, match="not completed retrieval"):
        reference(db, "fetches", {"id": "fetch-pending"})


# -------------------------------------------------------------------------- J2, J3


def test_contract_two_decision_requires_the_exact_discovery_pin(archive3):
    db, p = archive3
    migrate4(db)
    good = pinned_decision(db, p, 1)
    for change in (
        {"manifest": {}},
        {"manifest": {"discovery": {"id": 1, "digest": "0" * 64, "collector_config_hash": HASH}}},
        {
            "manifest": {
                "discovery": good["manifest"]["discovery"] | {"collector_config_hash": "b" * 64}
            }
        },
        {"manifest": {"discovery": good["manifest"]["discovery"] | {"id": 2}}},
        {"manifest": {"discovery": {"id": 1}}},
    ):
        with pytest.raises(ValueError):
            journal.append(db, "forecast_decisions", good | change)
    assert db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0] == 1
    assert journal.append(db, "forecast_decisions", good) == 2
    stored = json.loads(
        db.execute("SELECT payload_json FROM forecast_decisions WHERE id=2").fetchone()[0]
    )
    assert (
        stored["manifest"]["discovery"]["collector_config_hash"]
        == p["discovery_passes"]["config_hash"]
    )
    # Without a cited pass there must be no pin either.
    unpinned = good | {"discovery_id": None, "dataset": "no-pass", "cutoff_eligible": False}
    unpinned["reasons"] = ["universe_incomplete"]
    unpinned["results"] = {
        name: {"probability": None, "reasons": ["universe_incomplete"], "inputs": {}}
        for name in ("constant-v1", "midpoint-v1")
    }
    with pytest.raises(ValueError, match="pin without"):
        journal.append(db, "forecast_decisions", unpinned)
    unpinned["manifest"] = {}
    assert journal.append(db, "forecast_decisions", unpinned) == 3


def test_research_hash_rules_are_retained_under_contract_two(archive3):
    db, p = archive3
    migrate4(db)
    decision = pinned_decision(db, p, 1)
    with pytest.raises(ValueError, match="decision parent configuration"):
        journal.append(db, "forecast_decisions", decision | {"config_hash": "c" * 64})
    decision_id = journal.append(db, "forecast_decisions", decision)
    publication = p["forecast_publications"] | dict(
        decision_id=decision_id, decision_digest=digest(decision), config_hash="c" * 64
    )
    with pytest.raises(ValueError, match="publication configuration"):
        journal.append(db, "forecast_publications", publication)


def test_contract_two_decision_cannot_cite_or_supersede_contract_one(archive3):
    db, p = archive3
    migrate4(db)
    decision = pinned_decision(db, p, 1)
    decision["references"] = decision["references"] + [
        reference(db, "forecast_receipts", {"id": 1})
    ]
    with pytest.raises(ValueError, match="contract-1 receipt"):
        journal.append(db, "forecast_decisions", decision)
    correction = pinned_decision(db, p, 1, dataset="correction", supersedes_id=1, mode="synthetic")
    with pytest.raises(ValueError, match="supersede a contract-1"):
        journal.append(db, "forecast_decisions", correction)
    # N3: the contract-1 decision holds the scoring unit; same unit under contract 2 conflicts.
    same_unit = pinned_decision(db, p, 1, dataset=p["forecast_decisions"]["dataset"])
    with pytest.raises(journal.IdempotencyConflict):
        journal.append(db, "forecast_decisions", same_unit)
    assert db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0] == 1


# ------------------------------------------------------------------------- K1–K3


def test_mode_table_for_contract_two_receipts(archive3):
    db, p = archive3
    migrate4(db)
    with pytest.raises(ValueError, match="M4"):
        contracts.validate(
            "forecast_receipts", fetch_receipt(db, p, "fetch-2", mode="historical_reconstruction")
        )
    # M5: a forward receipt for a synthetic (fixture) fetch is forbidden.
    live = fetch_receipt(db, p, "fetch-2", mode="forward_shadow", run_id=LIVE_WITNESS)
    live["config_hash"] = digest(LIVE_CONFIG)
    with pytest.raises(ValueError):
        journal.append(db, "forecast_receipts", live)
    # M6: receipt → pass must match modes; S → S allowed, F → S forbidden.
    assert journal.append(db, "forecast_receipts", pass_receipt(db, p, 1)) == 3
    forward_pass_receipt = pass_receipt(db, p, 1, mode="forward_shadow", run_id=LIVE_WITNESS)
    forward_pass_receipt["config_hash"] = digest(LIVE_CONFIG)
    with pytest.raises(ValueError, match="incompatible parent mode|synthetic evidence"):
        journal.append(db, "forecast_receipts", forward_pass_receipt)
    # F → F for a live pass, observed by a live witness run.
    live_pass_id = journal.append(db, "discovery_passes", live_pass(db, p, digest(LIVE_CONFIG)))
    receipt = pass_receipt(db, p, live_pass_id, mode="forward_shadow", run_id=LIVE_WITNESS)
    receipt["config_hash"] = digest(LIVE_CONFIG)
    assert journal.append(db, "forecast_receipts", receipt) == 4
    # The same observation claimed by the live *collection* run is not witness proof.
    supplied = pass_receipt(db, p, live_pass_id, mode="forward_shadow", run_id=LIVE_RUN)
    supplied["config_hash"] = digest(LIVE_CONFIG)
    supplied["session_id"] = "collector-session"
    with pytest.raises(ValueError, match="operational witness run"):
        journal.append(db, "forecast_receipts", supplied)


@pytest.mark.parametrize(
    "decision_mode,binding_mode,pass_mode,accepted",
    [
        ("synthetic", "synthetic", "synthetic", True),
        ("forward_shadow", "forward_shadow", "forward_shadow", True),
        ("historical_reconstruction", "forward_shadow", "forward_shadow", True),  # K2
        ("historical_reconstruction", "historical_reconstruction", "forward_shadow", True),
        ("forward_shadow", "historical_reconstruction", "forward_shadow", False),  # M9 F→H
        ("forward_shadow", "forward_shadow", "synthetic", False),  # M10 F→S
        ("synthetic", "forward_shadow", "forward_shadow", False),  # M9 S→F
        ("historical_reconstruction", "forward_shadow", "synthetic", False),  # M10 H→S
    ],
)
def test_mode_table_for_contract_two_decisions(
    archive3, decision_mode, binding_mode, pass_mode, accepted
):
    db, p = archive3
    migrate4(db)
    research_hash = digest({"protocol": "mlb-t60-v1"})
    if pass_mode == "synthetic":
        pass_id = 1
    else:
        pass_id = journal.append(db, "discovery_passes", live_pass(db, p, digest(LIVE_CONFIG)))
    if binding_mode == "synthetic":
        binding_id = 1
        binding_hash = p["forecast_bindings"]["config_hash"]
    else:
        binding = live_binding(db, p, research_hash) | {"mode": binding_mode}
        binding_id = journal.append(db, "forecast_bindings", binding)
        binding_hash = research_hash
    decision = pinned_decision(db, p, pass_id, mode=decision_mode, binding_id=binding_id)
    decision["config_hash"] = binding_hash
    decision["run_id"] = "synthetic-run" if decision_mode == "synthetic" else LIVE_RUN
    if decision_mode != "synthetic":
        decision["references"] = []
    if accepted:
        decision_id = journal.append(db, "forecast_decisions", decision)
        assert decision_id >= 2
        if decision_mode == "historical_reconstruction":
            # K2: a reconstruction can never be published.
            publication = p["forecast_publications"] | dict(
                decision_id=decision_id,
                decision_digest=digest(decision),
                mode="historical_reconstruction",
                run_id=LIVE_RUN,
                references=[],
                config_hash=binding_hash,
            )
            with pytest.raises(ValueError, match="invalid publication mode"):
                journal.append(db, "forecast_publications", publication)
    else:
        with pytest.raises(ValueError):
            journal.append(db, "forecast_decisions", decision)
    assert journal.integrity(db)["integrity"] == "verified"


# --------------------------------------------------------------------------- J4


def evaluation_for(db, p, decisions):
    items = []
    for n, (decision_id, decision) in enumerate(decisions, 1):
        item = copy.deepcopy(p["evaluation_items"]) | dict(
            opportunity_key=f"game-{n}",
            game_id=decision["game_id"],
            decision_id=decision_id,
            publication_id=None,
            receipt_id=None,
            publication_status="not_attempted",
            publication_reasons=[],
            cutoff_status="eligible" if decision["cutoff_eligible"] else "ineligible",
            cutoff_reasons=decision["reasons"],
        )
        item["references"] = [reference(db, "settlement_versions", {"id": 3})]
        items.append(item)
    report = copy.deepcopy(p["evaluation_runs"]) | dict(
        dataset="contract-2-study",
        outcomes_as_of="2026-09-30T19:00:05+00:00",
        population_manifest=[
            {k: item[k] for k in ("opportunity_key", "decision_id", "game_id", "horizon")}
            for item in items
        ],
        publication_manifest=[],
    )
    report["publication_digest"] = digest([])
    report["report"] = {"items_digest": digest(items), "synthetic": True}
    return report, items


def test_evaluation_cohort_has_exactly_one_collector_configuration_hash(archive3):
    db, p = archive3
    migrate4(db)
    other_collector = p["discovery_passes"] | dict(
        session_id="other-collector", config_hash="d" * 64
    )
    other_pass = journal.append(db, "discovery_passes", other_collector)
    first = pinned_decision(db, p, 1)
    second = pinned_decision(db, p, other_pass, game_id=900002, ticker="KXMLBGAME-SYNTHETIC-B")
    same = pinned_decision(db, p, 1, game_id=900003, ticker="KXMLBGAME-SYNTHETIC-C")
    ids = {d["game_id"]: journal.append(db, "forecast_decisions", d) for d in (first, second, same)}
    before = snapshot(db)
    report, items = evaluation_for(db, p, [(ids[900001], first), (ids[900002], second)])
    with pytest.raises(ValueError, match="multiple collector configuration hashes"):
        journal.append_evaluation(db, report, items)
    assert snapshot(db) == before  # rejected atomically, nothing pooled
    report, items = evaluation_for(db, p, [(ids[900001], first), (ids[900003], same)])
    evaluation_id = journal.append_evaluation(db, report, items)
    assert evaluation_id == 2
    report2, items2 = evaluation_for(db, p, [(ids[900002], second)])
    assert journal.append_evaluation(db, report2, items2) == 3  # separately reported cohort
    assert journal.integrity(db)["integrity"] == "verified"


def test_evaluation_cannot_mix_contract_one_and_contract_two_decisions(archive3):
    db, p = archive3
    migrate4(db)
    pinned = pinned_decision(
        db, p, 1, game_id=900002, ticker="KXMLBGAME-SYNTHETIC-B", dataset="synthetic-study"
    )
    pinned_id = journal.append(db, "forecast_decisions", pinned)
    report, items = evaluation_for(db, p, [(1, p["forecast_decisions"]), (pinned_id, pinned)])
    report["dataset"] = "synthetic-study"
    before = snapshot(db)
    with pytest.raises(ValueError, match="mixes contract-1 and contract-2"):
        journal.append_evaluation(db, report, items)
    assert snapshot(db) == before
    # The pre-existing contract-1 evaluation of the contract-1 decision stays valid.
    assert journal.integrity(db)["record_contracts"]["evaluation_runs"] == {"1": 1, "2": 0}


def test_research_hash_change_is_distinct_from_collector_provenance(archive3):
    db, p = archive3
    migrate4(db)
    decision = pinned_decision(db, p, 1)
    decision_id = journal.append(db, "forecast_decisions", decision)
    stored = json.loads(
        db.execute(
            "SELECT payload_json FROM forecast_decisions WHERE id=?", (decision_id,)
        ).fetchone()[0]
    )
    assert stored["config_hash"] == p["forecast_bindings"]["config_hash"]  # research protocol
    assert (
        stored["manifest"]["discovery"]["collector_config_hash"]
        == p["discovery_passes"]["config_hash"]
    )
    # Changing the research hash alone makes a different scoring-unit key, not a conflict,
    # and does not alter the pinned collector hash.
    research_changed = decision | {"config_hash": digest({"protocol": "mlb-t60-v2"})}
    research_changed["binding_id"] = None
    research_changed["cutoff_eligible"] = False
    research_changed["reasons"] = ["identity_unproven"]
    research_changed["results"] = {
        name: {"probability": None, "reasons": ["identity_unproven"], "inputs": {}}
        for name in ("constant-v1", "midpoint-v1")
    }
    research_changed["ticker"] = research_changed["yes_team_id"] = None
    other = journal.append(db, "forecast_decisions", research_changed)
    assert other != decision_id
    assert (
        json.loads(
            db.execute(
                "SELECT payload_json FROM forecast_decisions WHERE id=?", (other,)
            ).fetchone()[0]
        )["manifest"]["discovery"]["collector_config_hash"]
        == p["discovery_passes"]["config_hash"]
    )


def test_envelopes_fixture_still_builds_on_schema_four(tmp_path):
    """The shipped fixture envelopes are contract 1; on schema 4 they are closed (N6)."""
    db = legacy(tmp_path / "fresh4.sqlite3")
    schema.migrate(db)
    migrate4(db)
    with pytest.raises(ValueError, match="closed"):
        envelopes(db)
    db.close()
