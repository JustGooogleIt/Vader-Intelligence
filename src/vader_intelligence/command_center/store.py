"""The Command Center store: durable runs, commands with receipts, run-task bindings and
pane registrations. A separate SQLite file in the orchestration root; the pods store and
the archive are never written through here.

Schema `cc1`. `command_events` is append-only (trigger-protected). Commands are inserted
once by the server (`accepted`) and applied by the controller (`applied` / `rejected`);
the same `command_id` is idempotent (same content → no-op, different content → refused).
"""

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "cc1"
STORE_NAME = "command-center.sqlite3"
RUN_KINDS = ("game_research", "maintenance")
RUN_STATES = ("created", "running", "paused", "canceling", "canceled", "completed", "failed")
COMMAND_KINDS = ("start", "pause", "resume", "cancel")
COMMAND_STATES = ("accepted", "applied", "rejected")
TERMINAL_RUN_STATES = ("canceled", "completed", "failed")

DDL = """
CREATE TABLE IF NOT EXISTS cc_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('game_research','maintenance')),
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    pins_json TEXT NOT NULL,
    template TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    pod_id TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    stage INTEGER NOT NULL DEFAULT 0,
    finished_at REAL,
    outcome TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS run_tasks(
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    stage INTEGER NOT NULL,
    PRIMARY KEY(run_id, task_id)
);
CREATE TABLE IF NOT EXISTS commands(
    command_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    run_id TEXT,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    received_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('accepted','applied','rejected')),
    reason TEXT,
    applied_at REAL
);
CREATE TABLE IF NOT EXISTS command_events(
    id INTEGER PRIMARY KEY,
    command_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER IF NOT EXISTS command_events_no_update BEFORE UPDATE ON command_events
    BEGIN SELECT RAISE(ABORT, 'command_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS command_events_no_delete BEFORE DELETE ON command_events
    BEGIN SELECT RAISE(ABORT, 'command_events is append-only'); END;
CREATE TABLE IF NOT EXISTS workers(
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    worker_id TEXT NOT NULL,
    role TEXT NOT NULL,
    attempt_id TEXT,
    pane_id TEXT,
    pane_pid TEXT,
    pane_token TEXT,
    registered_at REAL NOT NULL,
    PRIMARY KEY(run_id, worker_id)
);
"""


class CommandConflict(ValueError):
    """The same command id was received with different content."""


def epoch(now=None):
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).timestamp()
    return float(now)


def iso(seconds):
    if seconds is None:
        return None
    return datetime.fromtimestamp(float(seconds), timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def new_id():
    return str(uuid.uuid4())


class CenterStore:
    """Writes are serialized by SQLite (`BEGIN IMMEDIATE`, busy timeout): the server
    records commands and the controller applies them through the same file. Only one
    controller runs per root, guaranteed by the pods coordinator's lock, not by this
    store. `readonly=True` never writes."""

    def __init__(self, root, *, readonly=False):
        self.root = Path(root)
        self.path = self.root / STORE_NAME
        self.readonly = readonly
        self._closed = False
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(str(self.path))
            self.db = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.25,
                isolation_level=None,
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only=ON")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(DDL)
        row = self.db.execute("SELECT value FROM cc_meta WHERE key='schema'").fetchone()
        if row is None:
            self.db.execute("INSERT OR IGNORE INTO cc_meta VALUES ('schema', ?)", (SCHEMA,))
        elif row[0] != SCHEMA:
            raise RuntimeError(f"command center store schema {row[0]} is not {SCHEMA}")

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _write(self):
        if self.readonly:
            raise RuntimeError("read-only command center store")

    # Commands --------------------------------------------------------------------------

    def receive_command(self, command_id, kind, run_id, payload, now=None):
        """Insert-once: returns True when stored, False when the identical command already
        exists; refuses a different command under the same id."""
        self._write()
        if not isinstance(command_id, str) or len(command_id) > 64 or not command_id.strip():
            raise ValueError("command_id must be a short non-empty string")
        if kind not in COMMAND_KINDS:
            raise ValueError(f"unknown command kind {kind!r}")
        record = {"kind": kind, "run_id": run_id, "payload": payload}
        d = digest(record)
        at = epoch(now)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT digest FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is not None:
                if row[0] == d:
                    return False
                raise CommandConflict("command id reused with different content")
            self.db.execute(
                "INSERT INTO commands(command_id,kind,run_id,payload_json,digest,received_at,state)"
                " VALUES (?,?,?,?,?,?,'accepted')",
                (command_id, kind, run_id, canonical(payload), d, at),
            )
            self.db.execute(
                "INSERT INTO command_events(command_id,kind,at,detail_json) VALUES (?,?,?,?)",
                (command_id, "accepted", at, canonical({"kind": kind, "run_id": run_id})),
            )
        return True

    def pending_commands(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM commands WHERE state='accepted' ORDER BY received_at, command_id"
            )
        ]

    def settle_command(self, command_id, state, reason=None, now=None, detail=None):
        self._write()
        if state not in ("applied", "rejected"):
            raise ValueError("a command settles as applied or rejected")
        at = epoch(now)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE commands SET state=?, reason=?, applied_at=? WHERE command_id=? AND state='accepted'",
                (state, reason, at, command_id),
            )
            self.db.execute(
                "INSERT INTO command_events(command_id,kind,at,detail_json) VALUES (?,?,?,?)",
                (command_id, state, at, canonical({"reason": reason, **(detail or {})})),
            )

    def command(self, command_id):
        row = self.db.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
        return None if row is None else dict(row)

    def command_events(self, command_id):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM command_events WHERE command_id=? ORDER BY id", (command_id,)
            )
        ]

    # Runs ------------------------------------------------------------------------------

    def create_run(self, run_id, kind, pins, template, limits, pod_id, now=None):
        self._write()
        if kind not in RUN_KINDS:
            raise ValueError(f"unknown run kind {kind!r}")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO runs(run_id,kind,created_at,state,pins_json,template,limits_json,pod_id)"
                " VALUES (?,?,?,'created',?,?,?,?)",
                (run_id, kind, epoch(now), canonical(pins), template, canonical(limits), pod_id),
            )
        return run_id

    def set_run(
        self, run_id, *, state=None, paused=None, stage=None, outcome=None, detail=None, now=None
    ):
        self._write()
        sets, values = [], []
        if state is not None:
            if state not in RUN_STATES:
                raise ValueError(f"unknown run state {state!r}")
            sets.append("state=?")
            values.append(state)
            if state in TERMINAL_RUN_STATES:
                sets.append("finished_at=?")
                values.append(epoch(now))
        if paused is not None:
            sets.append("paused=?")
            values.append(int(bool(paused)))
        if stage is not None:
            sets.append("stage=?")
            values.append(int(stage))
        if outcome is not None:
            sets.append("outcome=?")
            values.append(outcome)
        if detail is not None:
            sets.append("detail_json=?")
            values.append(canonical(detail))
        if not sets:
            return
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", (*values, run_id))

    def bind_task(self, run_id, task_id, role, stage):
        self._write()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT OR IGNORE INTO run_tasks(run_id,task_id,role,stage) VALUES (?,?,?,?)",
                (run_id, task_id, role, int(stage)),
            )

    def run(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else self._run(row)

    def runs(self, *, kind=None, states=None, limit=100):
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(states)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.execute(
            f"SELECT * FROM runs {where} ORDER BY created_at DESC, run_id LIMIT ?", (*params, limit)
        )
        return [self._run(r) for r in rows]

    def _run(self, row):
        data = dict(row)
        data["pins"] = json.loads(data.pop("pins_json"))
        data["limits"] = json.loads(data.pop("limits_json"))
        data["detail"] = json.loads(data.pop("detail_json"))
        data["created_at"] = iso(data["created_at"])
        data["finished_at"] = iso(data["finished_at"])
        data["paused"] = bool(data["paused"])
        data["tasks"] = [
            dict(r)
            for r in self.db.execute(
                "SELECT task_id, role, stage FROM run_tasks WHERE run_id=? ORDER BY stage, role",
                (data["run_id"],),
            )
        ]
        return data

    def task_run(self, task_id):
        row = self.db.execute("SELECT run_id FROM run_tasks WHERE task_id=?", (task_id,)).fetchone()
        return None if row is None else row[0]

    # Workers / panes -------------------------------------------------------------------

    def register_worker(self, run_id, worker_id, role, attempt_id, pane, now=None):
        self._write()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT OR REPLACE INTO workers(run_id,worker_id,role,attempt_id,pane_id,pane_pid,"
                "pane_token,registered_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    worker_id,
                    role,
                    attempt_id,
                    pane.get("pane_id"),
                    pane.get("pane_pid"),
                    pane.get("pane_token"),
                    epoch(now),
                ),
            )

    def workers(self, run_id):
        return [
            dict(r) | {"registered_at": iso(r["registered_at"])}
            for r in self.db.execute(
                "SELECT * FROM workers WHERE run_id=? ORDER BY worker_id", (run_id,)
            )
        ]

    def worker(self, run_id, worker_id):
        row = self.db.execute(
            "SELECT * FROM workers WHERE run_id=? AND worker_id=?", (run_id, worker_id)
        ).fetchone()
        return None if row is None else dict(row) | {"registered_at": iso(row["registered_at"])}


def open_center(root, *, readonly=False):
    return CenterStore(root, readonly=readonly)
