"""Research registry: datasets, models, experiments, evaluations and the holdout ledger.

A separate SQLite file (`research.sqlite3`) and a content-addressed, write-once artifact
directory under one root, distinct from every evidence archive. All tables are
append-only (triggers). Identities are SHA-256 digests of canonical content, so
registering the same dataset, model or experiment twice returns the existing record and
a different content under the same identity is impossible.

The holdout ledger makes final-holdout use explicit: the first evaluation of a holdout is
recorded as its consumption, and every later evaluation of the same holdout (by any
model) is recorded as a reuse; reports read the ledger and never call a consumed holdout
untouched. Research pods and model searches receive no handle to this ledger.
"""

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from ..provenance import json_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
    rows_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY, family TEXT NOT NULL, dataset_id TEXT NOT NULL REFERENCES datasets(id),
    created_at TEXT NOT NULL, summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, dataset_id TEXT REFERENCES datasets(id),
    created_at TEXT NOT NULL, manifest_json TEXT NOT NULL, result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY, model_id TEXT NOT NULL REFERENCES models(id),
    dataset_id TEXT NOT NULL REFERENCES datasets(id), split TEXT NOT NULL,
    selection_biased INTEGER NOT NULL, created_at TEXT NOT NULL, metrics_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdout_ledger (
    id INTEGER PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
    holdout_key TEXT NOT NULL, model_id TEXT NOT NULL REFERENCES models(id),
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id), kind TEXT NOT NULL
    CHECK (kind IN ('consumed', 'reused')), at TEXT NOT NULL,
    UNIQUE(dataset_id, holdout_key, model_id)
);
CREATE TABLE IF NOT EXISTS holdout_pins (
    dataset_id TEXT PRIMARY KEY REFERENCES datasets(id), holdout_from TEXT NOT NULL,
    actor TEXT NOT NULL, at TEXT NOT NULL
);
"""
TABLES = ("datasets", "models", "experiments", "evaluations", "holdout_ledger", "holdout_pins")


def digest_of(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Registry:
    def __init__(self, root, *, readonly=False):
        self.readonly = readonly
        self.root = Path(root).resolve()
        self.artifacts = self.root / "artifacts"
        self.path = self.root / "research.sqlite3"
        if readonly:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
            self.db.execute("PRAGMA query_only=ON")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.artifacts.mkdir(exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            with self._lock():
                self.db.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
                for table in TABLES:
                    for event in ("UPDATE", "DELETE"):
                        self.db.execute(
                            f"CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()} BEFORE"
                            f" {event} ON {table} BEGIN SELECT RAISE(ABORT,"
                            f" 'append-only research registry'); END"
                        )
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    @contextmanager
    def _lock(self):
        with open(str(self.path) + ".lockfile", "a", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another process holds the research registry lock") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def transaction(self):
        with self._lock():
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ artifacts
    def put_artifact(self, data, suffix):
        """Write-once content-addressed bytes; returns the SHA-256."""
        sha = hashlib.sha256(data).hexdigest()
        path = self.artifacts / sha[:2] / f"{sha}{suffix}"
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise ValueError(f"artifact {sha} is corrupt on disk")
            return sha
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        return sha

    def get_artifact(self, sha, suffix):
        path = self.artifacts / sha[:2] / f"{sha}{suffix}"
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"artifact {sha} failed verification")
        return data

    # ------------------------------------------------------------------- datasets
    def register_dataset(self, manifest, rows):
        """Rows are stored as canonical JSON lines; the dataset id is the digest of the
        manifest (which records the rows' digest), so identical builds share one id."""
        data = "".join(json_text(row) + "\n" for row in rows).encode()
        rows_sha = hashlib.sha256(data).hexdigest()
        if manifest.get("rows_sha256") != rows_sha:
            raise ValueError("manifest rows_sha256 does not match the rows")
        dataset_id = digest_of(manifest)
        self.put_artifact(data, ".jsonl")
        with self.transaction():
            existing = self.db.execute(
                "SELECT manifest_json FROM datasets WHERE id=?", (dataset_id,)
            ).fetchone()
            if existing is None:
                self.db.execute(
                    "INSERT INTO datasets VALUES (?,?,?,?,?)",
                    (dataset_id, manifest["name"], now_iso(), rows_sha, json_text(manifest)),
                )
        return dataset_id

    def dataset(self, dataset_id):
        row = self.db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown dataset {dataset_id}")
        return json.loads(row["manifest_json"])

    def dataset_rows(self, dataset_id):
        manifest = self.dataset(dataset_id)
        data = self.get_artifact(manifest["rows_sha256"], ".jsonl")
        return [json.loads(line) for line in data.decode().splitlines()]

    # --------------------------------------------------------------------- models
    def register_model(self, artifact_json, *, family, dataset_id, summary):
        data = artifact_json.encode() if isinstance(artifact_json, str) else artifact_json
        model_id = self.put_artifact(data, ".json")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM models WHERE id=?", (model_id,)).fetchone() is None:
                self.db.execute(
                    "INSERT INTO models VALUES (?,?,?,?,?)",
                    (model_id, family, dataset_id, now_iso(), json_text(summary)),
                )
        return model_id

    def model_artifact(self, model_id):
        return self.get_artifact(model_id, ".json")

    # ---------------------------------------------------------------- experiments
    def record_experiment(self, kind, manifest, result, *, dataset_id=None):
        experiment_id = digest_of({"kind": kind, "manifest": manifest, "result": result})
        with self.transaction():
            if (
                self.db.execute("SELECT 1 FROM experiments WHERE id=?", (experiment_id,)).fetchone()
                is None
            ):
                self.db.execute(
                    "INSERT INTO experiments VALUES (?,?,?,?,?,?)",
                    (
                        experiment_id,
                        kind,
                        dataset_id,
                        now_iso(),
                        json_text(manifest),
                        json_text(result),
                    ),
                )
        return experiment_id

    # ---------------------------------------------------------------- evaluations
    def record_evaluation(self, model_id, dataset_id, split, metrics, *, selection_biased):
        with self.transaction():
            return self.db.execute(
                "INSERT INTO evaluations(model_id,dataset_id,split,selection_biased,created_at,"
                "metrics_json) VALUES (?,?,?,?,?,?)",
                (
                    model_id,
                    dataset_id,
                    split,
                    int(bool(selection_biased)),
                    now_iso(),
                    json_text(metrics),
                ),
            ).lastrowid

    def evaluate_holdout(self, model_id, dataset_id, holdout_key, metrics):
        """Record a final-holdout evaluation and its ledger entry in one transaction.

        Returns the ledger status: the first evaluation of this holdout consumes it; any
        later one (another model, or the same holdout reused) is recorded as a reuse. The
        same model cannot be evaluated on the same holdout twice."""
        with self.transaction():
            prior = self.db.execute(
                "SELECT COUNT(*) FROM holdout_ledger WHERE dataset_id=? AND holdout_key=?",
                (dataset_id, holdout_key),
            ).fetchone()[0]
            if self.db.execute(
                "SELECT 1 FROM holdout_ledger WHERE dataset_id=? AND holdout_key=? AND model_id=?",
                (dataset_id, holdout_key, model_id),
            ).fetchone():
                raise ValueError("this model was already evaluated on this holdout")
            evaluation_id = self.db.execute(
                "INSERT INTO evaluations(model_id,dataset_id,split,selection_biased,created_at,"
                "metrics_json) VALUES (?,?,?,?,?,?)",
                (
                    model_id,
                    dataset_id,
                    "holdout:" + holdout_key,
                    int(prior > 0),
                    now_iso(),
                    json_text(metrics),
                ),
            ).lastrowid
            self.db.execute(
                "INSERT INTO holdout_ledger(dataset_id,holdout_key,model_id,evaluation_id,kind,at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    dataset_id,
                    holdout_key,
                    model_id,
                    evaluation_id,
                    "consumed" if prior == 0 else "reused",
                    now_iso(),
                ),
            )
        return self.holdout_status(dataset_id, holdout_key) | {"evaluation_id": evaluation_id}

    def holdout_from(self, dataset_id):
        """First official date of the dataset's pinned final holdout, or None."""
        row = self.db.execute(
            "SELECT holdout_from FROM holdout_pins WHERE dataset_id=?", (dataset_id,)
        ).fetchone()
        return None if row is None else row[0]

    def pin_holdout(self, dataset_id, holdout_from, actor):
        """Pin the dataset's final holdout to official dates on or after `holdout_from`.

        Recorded once (append-only); pinning again to the same date is a no-op and to
        another date is refused."""
        date.fromisoformat(holdout_from)
        if not isinstance(actor, str) or not actor:
            raise ValueError("a pin needs an actor")
        with self.transaction():
            if (
                self.db.execute("SELECT 1 FROM datasets WHERE id=?", (dataset_id,)).fetchone()
                is None
            ):
                raise ValueError(f"unknown dataset {dataset_id}")
            row = self.db.execute(
                "SELECT holdout_from FROM holdout_pins WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if row is not None:
                if row[0] != holdout_from:
                    raise ValueError(f"dataset holdout is already pinned from {row[0]}")
                return holdout_from
            self.db.execute(
                "INSERT INTO holdout_pins(dataset_id,holdout_from,actor,at) VALUES (?,?,?,?)",
                (dataset_id, holdout_from, actor, now_iso()),
            )
        return holdout_from

    def holdout_evaluation(self, model_id, dataset_id, holdout_key):
        """The recorded holdout evaluation of `model_id` (ledger row and metrics), or None."""
        row = self.db.execute(
            "SELECT h.kind, h.at, h.evaluation_id, e.metrics_json FROM holdout_ledger h "
            "JOIN evaluations e ON e.id=h.evaluation_id "
            "WHERE h.dataset_id=? AND h.holdout_key=? AND h.model_id=?",
            (dataset_id, holdout_key, model_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "kind": row["kind"],
            "at": row["at"],
            "evaluation_id": row["evaluation_id"],
            "metrics": json.loads(row["metrics_json"]),
        }

    def holdout_status(self, dataset_id, holdout_key):
        rows = self.db.execute(
            "SELECT model_id, kind, at FROM holdout_ledger WHERE dataset_id=? AND holdout_key=?"
            " ORDER BY id",
            (dataset_id, holdout_key),
        ).fetchall()
        return {
            "holdout_key": holdout_key,
            "consumed": bool(rows),
            "untouched": not rows,
            "evaluations": len(rows),
            "first_consumed_at": rows[0]["at"] if rows else None,
            "models": [r["model_id"] for r in rows],
        }
