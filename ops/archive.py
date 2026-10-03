"""Read-only schema inspection and SQLite online backup to new paths."""

import hashlib
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from ops.common import absolute, sync_directory

# Verified against migrations 001/002/003. Future migrations require coordinated review.
COLUMNS = {
    "schema_migrations": {"version", "applied_at"},
    "runs": {
        "id",
        "kind",
        "started_at",
        "finished_at",
        "status",
        "provenance_json",
        "summary_json",
    },
    "blobs": {"sha256", "body", "byte_count"},
    "requests": {"id", "run_id", "stage", "request_key", "url", "params_json", "done"},
    "fetches": {"seq", "id", "request_id", "state", "body_sha256", "retrieved_at"},
    "entities": {"id", "fetch_id", "kind", "ticker", "parser_version", "data_json"},
    "observations": {"id", "fetch_id", "kind", "ticker", "parser_version", "data_json"},
    "eligibility": {"id", "run_id", "ticker", "eligible", "game_id", "reason"},
}
SETTLEMENT_COLUMNS = {
    "settlement_versions": {
        "id",
        "kind",
        "entity_key",
        "revision",
        "previous_id",
        "digest",
        "data_json",
        "change_kind",
        "first_fetch_id",
    },
    "settlement_observations": {"kind", "entity_key", "fetch_id", "version_id", "parser_version"},
    "settlement_targets": {
        "id",
        "run_id",
        "ticker",
        "terms_fetch_id",
        "market_fetch_id",
        "event_fetch_id",
        "schedule_fetch_id",
        "mapping_version_id",
        "state",
    },
}


def connect(path):
    path = absolute(path).resolve(strict=True)
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def schema(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (1, 2, 3):
        raise ValueError("expected supported collector schema version 1, 2 or 3")
    tables = COLUMNS | (SETTLEMENT_COLUMNS if version >= 2 else {})
    if version == 3:
        from vader_intelligence.forecast.schema import COLUMNS as FORECAST_COLUMNS
        from vader_intelligence.forecast.schema import check_schema

        check_schema(db)
        tables |= FORECAST_COLUMNS
    for table, required in tables.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            raise ValueError(f"missing expected schema columns in {table}")
    if db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() != [
        (v,) for v in range(1, version + 1)
    ]:
        raise ValueError("migration records do not match supported schema")
    return version, tables


def inspect(path):
    with closing(connect(path)) as db:
        db.execute("BEGIN")
        version, tables = schema(db)
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("foreign key check failed")
        for sha, body, length in db.execute("SELECT sha256,body,byte_count FROM blobs"):
            if len(body) != length or hashlib.sha256(body).hexdigest() != sha:
                raise ValueError("raw response hash/length mismatch")
        if version == 3:
            from vader_intelligence.forecast.journal import integrity

            integrity(db)
        return {
            "database": str(Path(path).resolve()),
            "schema_version": version,
            "integrity": "ok",
            "raw_hashes": "verified",
            "counts": {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
            },
        }


def backup(source, destination, *, timeout=300):
    """Also used for restores. An incomplete destination is retained and never reused."""
    source, destination = absolute(source), absolute(destination)
    started = time.monotonic()
    with closing(connect(source)) as src:
        src.execute("BEGIN")
        schema(src)
        # Exclusive creation refuses production, existing files, symlinks and hard links.
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(destination)) as dst:

            def progress(status, remaining, total):
                if time.monotonic() - started > timeout:
                    raise TimeoutError("backup deadline exceeded; retain incomplete destination")

            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            dst.execute("PRAGMA journal_mode=DELETE")
    result = inspect(destination)
    with destination.open("r+b") as stream:
        os.fsync(stream.fileno())
    sync_directory(destination.parent)
    result.update(source=str(source), runtime_seconds=time.monotonic() - started)
    return result


def classify(config, result, exit_code):
    """No book freshness heuristic. Corroborate successful CLI output against its run."""
    if not isinstance(result, dict) or result.get("errors") != []:
        return "failed_or_partial"
    status = result.get("status")
    if (exit_code, status) not in {(0, "complete"), (3, "inconclusive")}:
        return "failed_or_partial"
    with closing(connect(config["database"])) as db:
        db.execute("BEGIN")
        schema(db)
        row = db.execute(
            "SELECT kind,status,finished_at,summary_json FROM runs WHERE id=?",
            (result.get("run_id"),),
        ).fetchone()
        if not row or row[0] != "collect" or row[1] != status or not row[2]:
            return "unverified_result"
        import json

        if json.loads(row[3]) != result:
            return "unverified_result"
        after, through = result.get("eligibility_after_id"), result.get("eligibility_through_id")
        if (
            result.get("discovery_complete") is not True
            or type(after) is not int
            or type(through) is not int
            or not 0 <= after <= through
        ):
            return "unverified_result"
        eligible, games = db.execute(
            "SELECT COUNT(DISTINCT ticker),COUNT(DISTINCT game_id) FROM eligibility "
            "WHERE run_id=? AND id>? AND id<=? AND eligible=1",
            (result["run_id"], after, through),
        ).fetchone()
        if (
            type(result.get("eligible_contracts")) is not int
            or type(result.get("eligible_games")) is not int
            or (eligible, games) != (result["eligible_contracts"], result["eligible_games"])
        ):
            return "unverified_result"
    books = result.get("books_this_pass")
    if type(books) is not int:
        return "unverified_result"
    if exit_code == 0 and books > 0 and eligible > 0:
        return "collected_eligible_games"
    if exit_code == 3 and books == 0 and eligible == 0:
        return "discovery_no_eligible_games"
    return "inconclusive_with_eligible_games"
