"""Read-only schema inspection and SQLite online backup to new paths."""

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from ops.common import absolute, sync_directory

# Verified against 001_initial.sql. Future migrations require coordinated review.
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


def connect(path):
    path = absolute(path).resolve(strict=True)
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def schema(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise ValueError("expected collector schema version 1; coordinate future migrations")
    for table, required in COLUMNS.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            raise ValueError(f"missing expected schema columns in {table}")
    if db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() != [(1,)]:
        raise ValueError("expected only migration 1")


def inspect(path):
    with closing(connect(path)) as db:
        schema(db)
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("foreign key check failed")
        return {
            "database": str(Path(path).resolve()),
            "schema_version": 1,
            "integrity": "ok",
            "counts": {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in COLUMNS
            },
        }


def backup(source, destination, *, timeout=300):
    """Also used for restores. An incomplete destination is retained and never reused."""
    source, destination = absolute(source), absolute(destination)
    started = time.monotonic()
    with closing(connect(source)) as src:
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
        eligible = db.execute(
            "SELECT COUNT(*) FROM eligibility WHERE run_id=? AND eligible=1",
            (result["run_id"],),
        ).fetchone()[0]
    books = result.get("books_this_pass")
    if type(books) is not int:
        return "unverified_result"
    if exit_code == 0 and books > 0 and eligible > 0:
        return "collected_eligible_games"
    if exit_code == 3 and books == 0 and eligible == 0:
        return "discovery_no_eligible_games"
    return "inconclusive_with_eligible_games"
