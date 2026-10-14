"""Explicit schema-2 to schema-3 expansion; no Store/Unix imports or backfill."""

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import files

PROJECTIONS = {
    "discovery_passes": ("run_id", "session_id"),
    "forecast_bindings": (
        "run_id",
        "candidate_key",
        "policy_version",
        "config_hash",
        "mode",
        "revision",
        "previous_id",
    ),
    "forecast_decisions": (
        "run_id",
        "mode",
        "protocol",
        "cutoff",
        "binding_id",
        "discovery_id",
        "supersedes_id",
    ),
    "forecast_publications": ("run_id", "decision_id", "verdict"),
    "forecast_receipts": (
        "run_id",
        "subject_kind",
        "fetch_id",
        "binding_id",
        "publication_id",
        "discovery_id",
        "observed_at",
    ),
    "evaluation_runs": ("run_id", "mode", "view", "supersedes_id", "created_at"),
    "evaluation_items": (
        "evaluation_id",
        "opportunity_key",
        "game_id",
        "horizon",
        "decision_id",
        "publication_id",
        "receipt_id",
        "mapping_id",
        "mlb_id",
        "kalshi_id",
    ),
}
COLUMNS = {
    table: set(cols)
    | {"digest", "payload_json"}
    | (set() if table == "evaluation_items" else {"id", "idempotency_key"})
    for table, cols in PROJECTIONS.items()
}


SUPPORTED = (3, 4)
LEDGERS = {3: [1, 2, 3], 4: [1, 2, 3, 4]}


def migration_sql(name="003_forecast_evaluation.sql"):
    return files("vader_intelligence").joinpath("migrations/" + name).read_text()


def executable(script):
    """The SQL text with comments removed; migration 004 must have none."""
    return "\n".join(
        line for line in script.splitlines() if line.strip() and not line.lstrip().startswith("--")
    ).strip()


def statements(script):
    """Execute DDL without executescript's implicit pre-transaction COMMIT."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise ValueError("incomplete migration SQL")


def require_schema(db):
    if db.execute("PRAGMA user_version").fetchone()[0] not in SUPPORTED:
        raise ValueError("forecast storage requires explicit schema 3 migration")
    layout = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='evaluation_items'"
    ).fetchone()
    # Schema 3 is still a draft. Reject its pre-fix rowid layout on reads AND
    # writes; changing user_version or reopening must never silently rebuild it.
    if not layout or not "".join(layout[0].split()).rstrip(";").upper().endswith("WITHOUTROWID"):
        raise ValueError(
            "unsupported schema-3 layout: evaluation_items requires WITHOUT ROWID; "
            "preserve the pre-fix archive and create a separate database from schema 2"
        )


def check_schema(db):
    require_schema(db)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    ledger = [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
    if ledger != LEDGERS[version]:
        raise ValueError("forecast migration ledger mismatch")
    for table, columns in COLUMNS.items():
        if not columns <= {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            raise ValueError("incomplete forecast schema: " + table)
        for suffix in ("update", "delete", "replace"):
            if (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                    (table + "_no_" + suffix,),
                ).fetchone()
                is None
            ):
                raise ValueError("missing immutability trigger: " + table)


def check_contracts(db):
    """Stored record contracts must be known before any persistent change (plan §9.6).

    Cheap: reads only the payload's contract_version field, no reference re-hashing.
    Every receipt or decision declares contract 1 (absent) or the integer 2; no other
    table carries a contract field; contract 2 exists only on schema 4. Run on the
    actual connection before WAL, migration or any write so an invalid archive is
    rejected without being touched or legitimized.
    """
    version = db.execute("PRAGMA user_version").fetchone()[0]
    for table in COLUMNS:
        identity = "opportunity_key" if table == "evaluation_items" else "id"
        bad = db.execute(
            f"SELECT {identity}, json_extract(payload_json,'$.contract_version') FROM {table}"
            " WHERE json_extract(payload_json,'$.contract_version') IS NOT NULL AND NOT ("
            " json_type(payload_json,'$.contract_version')='integer'"
            " AND json_extract(payload_json,'$.contract_version')=2 AND ? ) LIMIT 1",
            (int(table in ("forecast_receipts", "forecast_decisions")),),
        ).fetchone()
        if bad:
            raise ValueError(f"{table} row {bad[0]}: unknown record contract version {bad[1]!r}")
        if version < 4 and table in ("forecast_receipts", "forecast_decisions"):
            row = db.execute(
                f"SELECT id FROM {table}"
                " WHERE json_extract(payload_json,'$.contract_version') IS NOT NULL LIMIT 1"
            ).fetchone()
            if row:
                raise ValueError(
                    f"{table} row {row[0]}: contract-2 record in a schema-{version} archive;"
                    " not migrated automatically"
                )


@contextmanager
def transaction(db, budget=30):
    """SQLite serialization. Production callers ALSO hold the existing writer_lock.

    Owns the connection's progress handler for this bounded transaction; use a
    dedicated connection. No hidden nested transaction or retry/commit.
    """
    if db.in_transaction or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("idle connection with foreign_keys=ON required")
    busy_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    db.execute(f"PRAGMA busy_timeout={min(busy_timeout, 5000)}")
    deadline = time.monotonic() + budget
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        db.execute("BEGIN IMMEDIATE")
        yield deadline
        if time.monotonic() >= deadline:
            raise TimeoutError("forecast storage transaction deadline")
        db.execute("COMMIT")
    except BaseException:
        db.set_progress_handler(None, 0)
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.set_progress_handler(None, 0)
        db.execute(f"PRAGMA busy_timeout={busy_timeout}")


def migrate(db, *, record_contract=None):
    """Idempotent explicit expansion. Plain: 2→3, no-op at 3 and 4. Schema 1 requires
    explicit settlement migration first.

    record_contract=2 performs migration 004 on a schema-3 archive: ledger row and
    user_version 4 in one transaction, no DDL, no row touched (plan §9.6). It is a
    no-op at 4 and refused below 3; nothing ever downgrades.
    """
    if record_contract not in (None, 2):
        raise ValueError("unsupported record contract; only --record-contract 2 exists")
    with transaction(db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 4:
            check_schema(db)
            check_contracts(db)
            return False
        if version == 3:
            check_schema(db)
            # A contract-2 row already present on schema 3, or an unknown contract, is an
            # invalid archive: refuse rather than legitimize it by advancing the version.
            check_contracts(db)
            if record_contract != 2:
                return False
            if executable(migration_sql("004_record_contract_2.sql")):
                raise ValueError("migration 004 must not contain executable SQL")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("legacy foreign key failure")
            db.execute(
                "INSERT INTO schema_migrations VALUES (4,?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.execute("PRAGMA user_version=4")
            check_schema(db)
            return True
        if version != 2:
            raise ValueError(
                "forecast migrate requires schema 2; on schema 1 run settlement migrate first"
            )
        if record_contract == 2:
            raise ValueError("record contract 2 requires schema 3; run forecast migrate first")
        expected = {
            "runs",
            "requests",
            "fetches",
            "blobs",
            "entities",
            "observations",
            "eligibility",
            "schema_migrations",
            "settlement_versions",
            "settlement_observations",
            "settlement_targets",
        }
        existing = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not expected <= existing:
            raise ValueError("incomplete legacy schema")
        if [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")] != [
            1,
            2,
        ]:
            raise ValueError("legacy migration ledger mismatch")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("legacy foreign key failure")
        for statement in statements(migration_sql()):
            db.execute(statement)
        db.execute(
            "INSERT INTO schema_migrations VALUES (3,?)", (datetime.now(timezone.utc).isoformat(),)
        )
        db.execute("PRAGMA user_version=3")
        check_schema(db)
    return True
