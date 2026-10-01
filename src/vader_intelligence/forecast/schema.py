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


def migration_sql():
    return (
        files("vader_intelligence").joinpath("migrations/003_forecast_evaluation.sql").read_text()
    )


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
    if db.execute("PRAGMA user_version").fetchone()[0] != 3:
        raise ValueError("forecast storage requires explicit schema 3 migration")


def check_schema(db):
    require_schema(db)
    if [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")] != [
        1,
        2,
        3,
    ]:
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


def migrate(db):
    """Idempotent 2→3 only. Schema 1 requires explicit settlement migration first."""
    with transaction(db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 3:
            check_schema(db)
            return False
        if version != 2:
            raise ValueError(
                "forecast migrate requires schema 2; on schema 1 run settlement migrate first"
            )
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
