import fcntl
import hashlib
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from .provenance import json_text


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value=None):
    return (value or utc_now()).isoformat()


@contextmanager
def writer_lock(path):
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Keep the inode: unlinking a held lock would allow a second independent lock.
    with open(str(p) + ".lockfile", "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another collector holds the database writer lock") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, path, *, min_free_bytes=512 * 1024 * 1024, readonly=False):
        self.path = Path(path).resolve()
        self.min_free_bytes = min_free_bytes
        if readonly:
            self.db = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only=ON")
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in (1, 2):
                self.db.close()
                raise RuntimeError("unsupported database schema")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.check_space()
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA fullfsync=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        try:
            self.migrate()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def check_space(self):
        if shutil.disk_usage(self.path.parent).free < self.min_free_bytes:
            raise RuntimeError("insufficient disk space; collection stopped without deleting data")

    def migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise RuntimeError(f"unsupported database schema version {version}")
        if version == 0:
            script = files("vader_intelligence").joinpath("migrations/001_initial.sql").read_text()
            try:
                self.db.executescript("BEGIN IMMEDIATE;\n" + script)
                self.db.execute("INSERT INTO schema_migrations VALUES (1, ?)", (timestamp(),))
                self.db.execute("PRAGMA user_version=1")
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self):
        self.check_space()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def start_run(self, provenance, started_at=None):
        with self.transaction():
            self.db.execute(
                "INSERT INTO runs(id,kind,started_at,status,provenance_json) VALUES (?,?,?,?,?)",
                (
                    provenance.run_id,
                    provenance.kind,
                    started_at or timestamp(),
                    "running",
                    provenance.to_json(),
                ),
            )

    def finish_run(self, run_id, status, summary):
        with self.transaction():
            self.db.execute(
                "UPDATE runs SET finished_at=?,status=?,summary_json=? WHERE id=?",
                (timestamp(), status, json_text(summary), run_id),
            )

    def recover(self, run_id):
        with self.transaction():
            self.db.execute(
                "UPDATE fetches SET state='interrupted',error='process interrupted' "
                "WHERE state='pending' AND request_id IN (SELECT id FROM requests WHERE run_id=?)",
                (run_id,),
            )
            self.db.execute(
                "UPDATE runs SET status='running',finished_at=NULL WHERE id=?", (run_id,)
            )

    def request(self, run_id, stage, key, url, params):
        identifier = str(uuid.uuid4())
        with self.transaction():
            self.db.execute(
                "INSERT INTO requests(id,run_id,stage,request_key,url,params_json) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,stage,request_key) DO NOTHING",
                (identifier, run_id, stage, key, url, json_text(params)),
            )
            row = self.db.execute(
                "SELECT * FROM requests WHERE run_id=? AND stage=? AND request_key=?",
                (run_id, stage, key),
            ).fetchone()
            if row["url"] != url or row["params_json"] != json_text(params):
                raise ValueError("request key reused with different input")
        return row

    def begin_fetch(self, request_id, started_at, fetch_id=None):
        identifier = fetch_id or str(uuid.uuid4())
        with self.transaction():
            attempt = self.db.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM fetches WHERE request_id=?", (request_id,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO fetches(id,request_id,attempt,started_at,state) VALUES (?,?,?,?,?)",
                (identifier, request_id, attempt, started_at, "pending"),
            )
        return identifier

    def complete_fetch(
        self,
        fetch_id,
        *,
        body,
        status,
        headers,
        retrieved_at,
        elapsed,
        error=None,
        truncated=False,
        normalizer=None,
    ):
        digest = hashlib.sha256(body).hexdigest() if body is not None else None
        parse_error = None
        with self.transaction():
            row = self.db.execute("SELECT * FROM fetches WHERE id=?", (fetch_id,)).fetchone()
            if row is None or row["state"] != "pending":
                raise ValueError("fetch completion requires a pending retrieval")
            if body is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO blobs VALUES (?,?,?)", (digest, body, len(body))
                )
            self.db.execute(
                "UPDATE fetches SET body_sha256=?,status=?,headers_json=?,retrieved_at=?,"
                "elapsed_seconds=?,error=?,truncated=?,state='error',source_api_version=? WHERE id=?",
                (
                    digest,
                    status,
                    json_text(headers),
                    retrieved_at,
                    elapsed,
                    error,
                    int(truncated),
                    "trade-api/v2" if headers.get("source") == "kalshi" else None,
                    fetch_id,
                ),
            )
            if status == 200 and not error and not truncated:
                self.db.execute("SAVEPOINT normalization")
                try:
                    checkpoint = normalizer(self.db, fetch_id, body) if normalizer else {}
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
                    self.db.execute("ROLLBACK TO normalization")
                    parse_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                    self.db.execute(
                        "UPDATE fetches SET state='parse_error',error=? WHERE id=?",
                        (parse_error, fetch_id),
                    )
                else:
                    self.db.execute("UPDATE fetches SET state='ok' WHERE id=?", (fetch_id,))
                    self.db.execute(
                        "UPDATE requests SET done=1,checkpoint_json=? WHERE id=?",
                        (json_text(checkpoint), row["request_id"]),
                    )
                finally:
                    self.db.execute("RELEASE normalization")
        return parse_error

    def saved(self, request_id):
        return self.db.execute(
            "SELECT f.*, b.body FROM fetches f JOIN blobs b ON b.sha256=f.body_sha256 "
            "WHERE request_id=? AND f.state='ok' ORDER BY seq DESC LIMIT 1",
            (request_id,),
        ).fetchone()

    def verify_integrity(self):
        failures = []
        for row in self.db.execute("SELECT sha256,body,byte_count FROM blobs"):
            if hashlib.sha256(row["body"]).hexdigest() != row["sha256"]:
                if len(failures) < 100:
                    failures.append("body hash mismatch: " + row["sha256"])
            if len(row["body"]) != row["byte_count"]:
                if len(failures) < 100:
                    failures.append("body length mismatch: " + row["sha256"])
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            failures.append("SQLite quick_check failed")
        if self.db.execute("PRAGMA foreign_key_check").fetchone():
            failures.append("foreign key check failed")
        return failures

    def summary(self, run_id):
        values = {"run_id": run_id}
        values["fetches"] = self.db.execute(
            "SELECT COUNT(*) FROM fetches f JOIN requests r ON r.id=f.request_id WHERE r.run_id=?",
            (run_id,),
        ).fetchone()[0]
        for table in ("entities", "observations"):
            values[table] = self.db.execute(
                f"SELECT COUNT(*) FROM {table} x JOIN fetches f ON x.fetch_id=f.id "
                "JOIN requests r ON r.id=f.request_id WHERE r.run_id=?",
                (run_id,),
            ).fetchone()[0]
        values["books"] = self.db.execute(
            "SELECT COUNT(*) FROM observations o JOIN fetches f ON o.fetch_id=f.id "
            "JOIN requests r ON r.id=f.request_id WHERE r.run_id=? AND o.kind='book'",
            (run_id,),
        ).fetchone()[0]
        values["eligibility_reasons"] = dict(
            self.db.execute(
                "SELECT reason,COUNT(*) FROM eligibility WHERE run_id=? GROUP BY reason", (run_id,)
            ).fetchall()
        )
        return values

    def health(self, stale_seconds=180):
        row = self.db.execute(
            "SELECT * FROM runs WHERE kind NOT IN ('fixture','settlement-refresh') "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        book = self.db.execute(
            "SELECT f.retrieved_at FROM observations o JOIN fetches f ON f.id=o.fetch_id "
            "JOIN requests req ON req.id=f.request_id JOIN runs r ON r.id=req.run_id "
            "WHERE o.kind='book' AND r.kind!='fixture' ORDER BY f.seq DESC LIMIT 1"
        ).fetchone()
        age = (utc_now() - datetime.fromisoformat(book[0])).total_seconds() if book else None
        free = shutil.disk_usage(self.path.parent).free
        return {
            "latest_run": dict(row) if row else None,
            "last_book_at": book[0] if book else None,
            "book_age_seconds": age,
            "stale": age is None or age < 0 or age > stale_seconds,
            "pending_fetches": self.db.execute(
                "SELECT COUNT(*) FROM fetches WHERE state='pending'"
            ).fetchone()[0],
            "interrupted_fetches": self.db.execute(
                "SELECT COUNT(*) FROM fetches WHERE state='interrupted'"
            ).fetchone()[0],
            "free_bytes": free,
            "storage_pressure": free < self.min_free_bytes,
            "database_bytes": sum(
                p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.is_file()
            ),
        }
