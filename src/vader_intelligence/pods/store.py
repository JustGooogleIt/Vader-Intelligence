"""SQLite orchestration store: the single source of truth for pods, tasks and attempts.

A separate file (`<root>/orchestration.sqlite3`) using the archive's conventions: WAL,
FULL synchronous commits, foreign keys, and short BEGIN IMMEDIATE transactions that
never span process spawning, file copying or any network access.

Invariants enforced by the schema itself (triggers), not only by this module:

- `task_events`, `results`, `artifacts`, `pods` and `budgets` are append-only.
- A task's identity columns never change; terminal states are absorbing; only legal
  edges are possible; a state change is refused unless the task's most recent event is
  the one that justifies it (so state and event are written in the same transaction).
- A finished attempt is immutable; a worker PID can be recorded once; at most one open
  attempt exists per task.

Workers write exactly two things, through `claim_attempt` and `renew_lease`: their PID
and their lease expiry (each with a `heartbeat` event), guarded by the lease token.
Everything else is written by the coordinator or by operator submission/cancellation.
"""

import hmac
import json
import math
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..provenance import json_text
from .artifacts import ArtifactMissing, ArtifactStore
from .contracts import (
    BUDGET_WINDOWS,
    TERMINAL_TASK_STATES,
    ContractError,
    ExperimentManifest,
    PodSpec,
    ResultManifest,
    TaskSpec,
    content_hash,
    require,
    task_identity,
)
from .operations import UnknownOperation, get_operation, normalized_params

SCHEMA_VERSION = 1
DB_NAME = "orchestration.sqlite3"
SHUTDOWN_REASON = "coordinator_shutdown"
DAY_SECONDS = 86400

SCHEMA = """
CREATE TABLE pods (
    pod_id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN
        ('baseline_models','symbolic_search','robustness_execution','diagnostics')),
    created_at REAL NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL CHECK(length(config_hash)=64)
);
CREATE TABLE budgets (
    pod_id TEXT NOT NULL REFERENCES pods(pod_id),
    kind TEXT NOT NULL CHECK(kind IN ('max_concurrent','cpu_seconds_per_day','tasks_per_day',
        'wall_seconds_per_task','memory_mb','api_cost_usd')),
    limit_value REAL NOT NULL CHECK(limit_value >= 0),
    window TEXT NOT NULL CHECK(window IN ('instant','utc_day','per_task')),
    PRIMARY KEY (pod_id, kind),
    CHECK(kind <> 'api_cost_usd' OR limit_value = 0)
);
CREATE TABLE artifacts (
    sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64),
    kind TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK(bytes >= 0),
    created_at REAL NOT NULL,
    source_task_id TEXT
);
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY CHECK(length(task_id)=32),
    pod_id TEXT NOT NULL REFERENCES pods(pod_id),
    type TEXT NOT NULL,
    params_json TEXT NOT NULL,
    params_hash TEXT NOT NULL CHECK(length(params_hash)=64),
    experiment_manifest_hash TEXT NOT NULL REFERENCES artifacts(sha256),
    priority INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 10),
    created_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','leased','retry_wait','succeeded','failed',
        'partial','canceled','quarantined')),
    not_before REAL,
    cancel_requested_at REAL
);
CREATE INDEX tasks_schedule ON tasks(state, priority DESC, created_at);
CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    n INTEGER NOT NULL CHECK(n >= 1),
    lease_token TEXT NOT NULL CHECK(length(lease_token)=32),
    worker_pid INTEGER,
    started_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    finished_at REAL,
    outcome TEXT CHECK(outcome IN ('succeeded','partial','failed','lost','canceled',
        'quarantined')),
    reason TEXT,
    cpu_seconds REAL NOT NULL DEFAULT 0,
    cpu_limit_seconds INTEGER NOT NULL,
    workdir TEXT NOT NULL,
    log_path TEXT NOT NULL,
    UNIQUE(task_id, n),
    CHECK((finished_at IS NULL) = (outcome IS NULL))
);
CREATE UNIQUE INDEX attempts_one_open ON attempts(task_id) WHERE finished_at IS NULL;
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT REFERENCES attempts(attempt_id),
    kind TEXT NOT NULL CHECK(kind IN ('submitted','leased','heartbeat','succeeded','failed',
        'lost','retry_scheduled','cancel_requested','canceled','quarantined','ingested')),
    at REAL NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX task_events_task ON task_events(task_id, id);
CREATE TABLE results (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    result_hash TEXT NOT NULL CHECK(length(result_hash)=64),
    result_json TEXT NOT NULL,
    ingested_at REAL NOT NULL
);
CREATE TABLE candidates (
    candidate_id TEXT PRIMARY KEY CHECK(length(candidate_id)=32),
    pod_id TEXT NOT NULL REFERENCES pods(pod_id),
    kind TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL CHECK(length(spec_hash)=64),
    source_task_id TEXT NOT NULL REFERENCES results(task_id),
    experiment_manifest_hash TEXT NOT NULL REFERENCES artifacts(sha256),
    created_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('proposed','validated','shadow_tested',
        'paper_approved','rejected','quarantined','retired'))
);
CREATE TABLE candidate_evidence (
    evidence_id TEXT PRIMARY KEY CHECK(length(evidence_id)=64),
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    kind TEXT NOT NULL CHECK(kind IN ('evaluator_metrics','shadow_evaluation','paper_report',
        'operator_approval','exit_decision')),
    payload_json TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE TABLE candidate_events (
    id INTEGER PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE holdout_ledger (
    candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id),
    evaluator_version TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    evaluation_hash TEXT NOT NULL CHECK(length(evaluation_hash)=64),
    consumed_at REAL NOT NULL
);
"""

APPEND_ONLY = (
    "pods",
    "budgets",
    "artifacts",
    "task_events",
    "results",
    "candidate_evidence",
    "candidate_events",
    "holdout_ledger",
)

TRIGGERS = (
    *(
        f"CREATE TRIGGER {table}_no_{op.lower()} BEFORE {op} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, 'append-only: {table}'); END;"
        for table in APPEND_ONLY
        for op in ("UPDATE", "DELETE")
    ),
    "CREATE TRIGGER tasks_no_delete BEFORE DELETE ON tasks "
    "BEGIN SELECT RAISE(ABORT, 'task history is append-only'); END;",
    "CREATE TRIGGER tasks_identity BEFORE UPDATE OF task_id, pod_id, type, params_json, "
    "params_hash, experiment_manifest_hash, priority, max_attempts, created_at ON tasks "
    "BEGIN SELECT RAISE(ABORT, 'task identity is immutable'); END;",
    "CREATE TRIGGER tasks_cancel_once BEFORE UPDATE OF cancel_requested_at ON tasks "
    "WHEN OLD.cancel_requested_at IS NOT NULL "
    "BEGIN SELECT RAISE(ABORT, 'cancellation is recorded once'); END;",
    """CREATE TRIGGER tasks_state_edges BEFORE UPDATE OF state ON tasks
    WHEN NEW.state IS NOT OLD.state AND NOT (
        (OLD.state IN ('queued','retry_wait') AND NEW.state IN ('leased','canceled')) OR
        (OLD.state = 'leased' AND NEW.state IN ('retry_wait','succeeded','partial','failed',
            'canceled','quarantined')))
    BEGIN SELECT RAISE(ABORT, 'illegal task state transition'); END;""",
    """CREATE TRIGGER tasks_state_event BEFORE UPDATE OF state ON tasks
    WHEN NEW.state IS NOT OLD.state AND COALESCE((SELECT kind FROM task_events
        WHERE task_id = NEW.task_id ORDER BY id DESC LIMIT 1), '') <> CASE NEW.state
        WHEN 'retry_wait' THEN 'retry_scheduled' WHEN 'succeeded' THEN 'ingested'
        WHEN 'partial' THEN 'ingested' ELSE NEW.state END
    BEGIN SELECT RAISE(ABORT, 'task state change requires its event'); END;""",
    "CREATE TRIGGER attempts_no_delete BEFORE DELETE ON attempts "
    "BEGIN SELECT RAISE(ABORT, 'attempt history is append-only'); END;",
    "CREATE TRIGGER attempts_finished_immutable BEFORE UPDATE ON attempts "
    "WHEN OLD.finished_at IS NOT NULL "
    "BEGIN SELECT RAISE(ABORT, 'finished attempt is immutable'); END;",
    "CREATE TRIGGER attempts_identity BEFORE UPDATE OF attempt_id, task_id, n, lease_token, "
    "started_at, deadline_at, cpu_limit_seconds, workdir, log_path ON attempts "
    "BEGIN SELECT RAISE(ABORT, 'attempt identity is immutable'); END;",
    "CREATE TRIGGER attempts_pid_once BEFORE UPDATE OF worker_pid ON attempts "
    "WHEN OLD.worker_pid IS NOT NULL AND NEW.worker_pid IS NOT OLD.worker_pid "
    "BEGIN SELECT RAISE(ABORT, 'worker pid is recorded once'); END;",
    "CREATE TRIGGER candidates_no_delete BEFORE DELETE ON candidates "
    "BEGIN SELECT RAISE(ABORT, 'candidate history is append-only'); END;",
    "CREATE TRIGGER candidates_identity BEFORE UPDATE OF candidate_id, pod_id, kind, spec_json, "
    "spec_hash, source_task_id, experiment_manifest_hash, created_at ON candidates "
    "BEGIN SELECT RAISE(ABORT, 'candidate identity is immutable'); END;",
    """CREATE TRIGGER candidates_state_edges BEFORE UPDATE OF state ON candidates
    WHEN NOT (
        (OLD.state = 'proposed' AND NEW.state IN ('validated','rejected','quarantined')) OR
        (OLD.state = 'validated' AND NEW.state IN ('shadow_tested','rejected','quarantined',
            'retired')) OR
        (OLD.state = 'shadow_tested' AND NEW.state IN ('paper_approved','rejected',
            'quarantined','retired')) OR
        (OLD.state = 'paper_approved' AND NEW.state IN ('quarantined','retired')))
    BEGIN SELECT RAISE(ABORT, 'illegal candidate transition'); END;""",
    """CREATE TRIGGER candidates_state_event BEFORE UPDATE OF state ON candidates
    WHEN NOT EXISTS (SELECT 1 FROM candidate_events WHERE id = (SELECT MAX(id)
        FROM candidate_events WHERE candidate_id = NEW.candidate_id)
        AND from_state = OLD.state AND to_state = NEW.state)
    BEGIN SELECT RAISE(ABORT, 'candidate transition requires its event'); END;""",
)


class StaleAttempt(ValueError):
    """The attempt is unknown or finished, or the lease token does not match."""


class IdempotencyConflict(ValueError):
    """An existing identity was reused with different content."""


def epoch_seconds(now) -> float:
    return time.time() if now is None else float(now)


def iso(seconds) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


@contextmanager
def immediate(db):
    """One short BEGIN IMMEDIATE transaction; no nesting, rollback on any exception."""
    if db.in_transaction:
        raise RuntimeError("nested orchestration transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def _connect(path: Path, *, readonly=False, synchronous="FULL") -> sqlite3.Connection:
    if readonly:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
        db.execute("PRAGMA query_only=ON")
    else:
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    if not readonly:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(f"PRAGMA synchronous={synchronous}")
        db.execute("PRAGMA fullfsync=ON")
    return db


def _event(db, task_id, kind, at, detail=None, attempt_id=None):
    db.execute(
        "INSERT INTO task_events(task_id, attempt_id, kind, at, detail_json) VALUES (?,?,?,?,?)",
        (task_id, attempt_id, kind, at, json_text(detail or {})),
    )


class OrchestrationStore:
    """Handle on one orchestration root: database, artifact store and attempt dirs.

    Use from a single thread. Writers other than the coordinator (submission,
    cancellation) are safe: every write is one short SQLite transaction.
    """

    def __init__(self, root, *, readonly=False):
        self.root = Path(root).resolve()
        self.path = self.root / DB_NAME
        self.readonly = readonly
        self.attempts_dir = self.root / "attempts"
        if readonly:
            self.db = _connect(self.path, readonly=True)
            self.artifacts = None
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.attempts_dir.mkdir(exist_ok=True)
            self.db = _connect(self.path)
            self.artifacts = ArtifactStore(self.root / "artifacts")
        try:
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version != 0 or self.readonly:
            raise RuntimeError(f"unsupported orchestration schema version {version}")
        script = "BEGIN IMMEDIATE;\n" + SCHEMA + "\n".join(TRIGGERS)
        try:
            self.db.executescript(script)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def transaction(self):
        return immediate(self.db)

    # Pods and manifests --------------------------------------------------------------

    def register_pod(self, pod: PodSpec, now=None) -> bool:
        """Create a pod; identical re-registration is a no-op, a changed config is refused."""
        config = pod.config()
        config_hash = content_hash(config)
        with self.transaction():
            row = self.db.execute(
                "SELECT config_hash FROM pods WHERE pod_id=?", (pod.pod_id,)
            ).fetchone()
            if row is not None:
                if row[0] != config_hash:
                    raise IdempotencyConflict(f"pod {pod.pod_id!r} exists with another config")
                return False
            self.db.execute(
                "INSERT INTO pods VALUES (?,?,?,?,?)",
                (pod.pod_id, pod.role, epoch_seconds(now), json_text(config), config_hash),
            )
            for kind, value in config["budgets"].items():
                self.db.execute(
                    "INSERT INTO budgets VALUES (?,?,?,?)",
                    (pod.pod_id, kind, value, BUDGET_WINDOWS[kind]),
                )
        return True

    def budgets(self, pod_id) -> dict:
        rows = self.db.execute("SELECT kind, limit_value FROM budgets WHERE pod_id=?", (pod_id,))
        integral = ("max_concurrent", "tasks_per_day", "memory_mb")
        return {kind: int(value) if kind in integral else value for kind, value in rows}

    def put_experiment_manifest(self, manifest: ExperimentManifest, now=None) -> str:
        """Store an experiment manifest as an immutable artifact; returns its sha256.

        The parameters are checked against the operation's contract and stored in their
        canonical form, so equivalent manifests always have the same hash.
        """
        try:
            params = normalized_params(manifest.operation, manifest.params)
        except UnknownOperation as exc:
            raise ContractError(str(exc)) from None
        data = replace(manifest, params=params).to_bytes()
        sha = self.artifacts.put_bytes(data)
        with self.transaction():
            self.db.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,NULL) ON CONFLICT(sha256) DO NOTHING",
                (sha, "experiment_manifest", len(data), epoch_seconds(now)),
            )
        return sha

    def put_input(self, data: bytes, kind: str, now=None) -> str:
        """Store a coordinator-built input artifact (e.g. a dataset snapshot); its sha256."""
        require(kind != "experiment_manifest", "use put_experiment_manifest")
        sha = self.artifacts.put_bytes(data)
        with self.transaction():
            row = self.db.execute("SELECT kind FROM artifacts WHERE sha256=?", (sha,)).fetchone()
            require(row is None or row[0] == kind, f"artifact {sha} is stored as {row and row[0]}")
            self.db.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,NULL) ON CONFLICT(sha256) DO NOTHING",
                (sha, kind, len(data), epoch_seconds(now)),
            )
        return sha

    def task_inputs(self, task_type: str, params) -> tuple:
        """The input artifacts an operation declares, each stored with the declared kind."""
        spec = get_operation(task_type)
        try:
            declared = tuple(spec.inputs(spec.parse(params), self))
        except (KeyError, ValueError) as exc:
            raise ContractError(f"inputs unavailable: {exc}") from None
        for sha, kind in declared:
            row = self.db.execute("SELECT kind FROM artifacts WHERE sha256=?", (sha,)).fetchone()
            require(row is not None, f"input artifact {sha} is not stored")
            require(row[0] == kind, f"input artifact {sha} is a {row[0]}, not a {kind}")
        return declared

    def experiment_manifest(self, sha256: str) -> ExperimentManifest:
        row = self.db.execute("SELECT kind FROM artifacts WHERE sha256=?", (sha256,)).fetchone()
        require(row is not None and row[0] == "experiment_manifest", "unknown experiment manifest")
        try:
            return ExperimentManifest.from_json(self.artifacts.get_bytes(sha256))
        except ArtifactMissing:
            raise ContractError("experiment manifest file is missing") from None

    # Submission and cancellation -----------------------------------------------------

    def submit(self, task: TaskSpec, now=None) -> str:
        """Validate and enqueue; resubmitting identical work returns the same task id.

        The pod's role must permit the operation, the parameters must satisfy its typed
        contract, and the cited experiment manifest must be stored and describe exactly
        this operation and these parameters.
        """
        try:
            spec = get_operation(task.type)
        except UnknownOperation as exc:
            raise ContractError(str(exc)) from None
        params = normalized_params(task.type, task.params)
        manifest = self.experiment_manifest(task.experiment_manifest_hash)
        require(manifest.operation == task.type, "experiment manifest names another operation")
        require(
            normalized_params(manifest.operation, manifest.params) == params,
            "experiment manifest parameters differ from the task parameters",
        )
        self.task_inputs(task.type, params)
        task_id = task_identity(task.type, params, task.experiment_manifest_hash)
        at = epoch_seconds(now)
        with self.transaction():
            pod = self.db.execute("SELECT role FROM pods WHERE pod_id=?", (task.pod_id,)).fetchone()
            require(pod is not None, f"unknown pod {task.pod_id!r}")
            require(pod[0] in spec.roles, f"pod role {pod[0]!r} may not run {task.type!r}")
            existing = self.db.execute(
                "SELECT pod_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != task.pod_id:
                    raise IdempotencyConflict("identical task already submitted to another pod")
                return task_id
            self.db.execute(
                "INSERT INTO tasks(task_id, pod_id, type, params_json, params_hash, "
                "experiment_manifest_hash, priority, max_attempts, created_at, state) "
                "VALUES (?,?,?,?,?,?,?,?,?,'queued')",
                (
                    task_id,
                    task.pod_id,
                    task.type,
                    json_text(params),
                    content_hash(params),
                    task.experiment_manifest_hash,
                    task.priority,
                    task.max_attempts,
                    at,
                ),
            )
            detail = {"pod_id": task.pod_id, "type": task.type, "priority": task.priority}
            _event(self.db, task_id, "submitted", at, detail)
        return task_id

    def request_cancel(self, task_id, now=None) -> str:
        """Record a cancellation; waiting tasks cancel at once, leased ones via the coordinator."""
        at = epoch_seconds(now)
        with self.transaction():
            row = self.db.execute(
                "SELECT state, cancel_requested_at FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] in TERMINAL_TASK_STATES or row["cancel_requested_at"] is not None:
                return row["state"]
            _event(self.db, task_id, "cancel_requested", at)
            self.db.execute("UPDATE tasks SET cancel_requested_at=? WHERE task_id=?", (at, task_id))
            if row["state"] == "leased":
                return "leased"
            _event(self.db, task_id, "canceled", at, {"reason": "canceled_before_lease"})
            self.db.execute(
                "UPDATE tasks SET state='canceled', not_before=NULL WHERE task_id=?", (task_id,)
            )
            return "canceled"

    # Queries -------------------------------------------------------------------------

    def task(self, task_id) -> dict | None:
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return None if row is None else dict(row)

    def attempts(self, task_id) -> list[dict]:
        rows = self.db.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY n", (task_id,))
        return [dict(row) for row in rows]

    def events(self, task_id) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, attempt_id, kind, at, detail_json FROM task_events "
            "WHERE task_id=? ORDER BY id",
            (task_id,),
        )
        return [
            {
                "id": row["id"],
                "attempt_id": row["attempt_id"],
                "kind": row["kind"],
                "at": row["at"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    def result(self, task_id) -> dict | None:
        row = self.db.execute("SELECT * FROM results WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json"))
        return value

    def open_attempts(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT a.*, t.pod_id, t.type, t.cancel_requested_at FROM attempts a "
            "JOIN tasks t USING(task_id) WHERE a.finished_at IS NULL ORDER BY a.started_at"
        )
        return [dict(row) for row in rows]

    def schedulable_tasks(self, now=None) -> list[dict]:
        rows = self.db.execute(
            "SELECT task_id, pod_id, type, priority FROM tasks WHERE cancel_requested_at IS NULL "
            "AND (state='queued' OR (state='retry_wait' AND not_before <= ?)) "
            "ORDER BY priority DESC, created_at, task_id",
            (epoch_seconds(now),),
        )
        return [dict(row) for row in rows]

    def waiting_retries(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM tasks WHERE state='retry_wait'").fetchone()[0]

    def pod_usage(self, pod_id, now=None) -> dict:
        """Open attempts, plus attempts started and CPU seconds used in the current UTC day."""
        day_start = math.floor(epoch_seconds(now) / DAY_SECONDS) * DAY_SECONDS
        active = self.db.execute(
            "SELECT COUNT(*) FROM attempts a JOIN tasks t USING(task_id) "
            "WHERE t.pod_id=? AND a.finished_at IS NULL",
            (pod_id,),
        ).fetchone()[0]
        started, cpu = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(a.cpu_seconds), 0) FROM attempts a "
            "JOIN tasks t USING(task_id) WHERE t.pod_id=? AND a.started_at >= ?",
            (pod_id, day_start),
        ).fetchone()
        return {"active": active, "attempts_today": started, "cpu_seconds_today": cpu}

    # Attempt lifecycle (coordinator only) --------------------------------------------

    def create_attempt(self, task_id, now, *, lease_seconds, deadline_at, limits) -> dict:
        """Lease a due task to a new attempt with a fresh token (process not yet spawned)."""
        at = epoch_seconds(now)
        with self.transaction():
            row = self.db.execute(
                "SELECT state, not_before, cancel_requested_at FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            due = row is not None and (
                row["state"] == "queued"
                or (row["state"] == "retry_wait" and row["not_before"] <= at)
            )
            if not due or row["cancel_requested_at"] is not None:
                raise StaleAttempt(f"task {task_id} is not schedulable")
            n = self.db.execute(
                "SELECT COUNT(*) + 1 FROM attempts WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            if n > 999:
                raise RuntimeError(f"task {task_id} exhausted attempt numbering")
            attempt_id = f"{task_id}-{n}"
            workdir = self.attempts_dir / attempt_id
            self.db.execute(
                "INSERT INTO attempts(attempt_id, task_id, n, lease_token, started_at, "
                "lease_expires_at, deadline_at, cpu_limit_seconds, workdir, log_path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    task_id,
                    n,
                    secrets.token_hex(16),
                    at,
                    at + lease_seconds,
                    deadline_at,
                    limits["cpu_seconds"],
                    str(workdir),
                    str(workdir / "worker.log"),
                ),
            )
            detail = {"n": n, "deadline_at": deadline_at, "limits": limits}
            _event(self.db, task_id, "leased", at, detail, attempt_id)
            self.db.execute(
                "UPDATE tasks SET state='leased', not_before=NULL WHERE task_id=?", (task_id,)
            )
            return dict(
                self.db.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            )

    def _attempt_with_task(self, attempt_id):
        return self.db.execute(
            "SELECT a.*, t.state, t.max_attempts, t.cancel_requested_at FROM attempts a "
            "JOIN tasks t USING(task_id) WHERE a.attempt_id=?",
            (attempt_id,),
        ).fetchone()

    def _close(self, attempt_id, outcome, reason, at, cpu_seconds):
        self.db.execute(
            "UPDATE attempts SET finished_at=?, outcome=?, reason=?, "
            "cpu_seconds=MAX(cpu_seconds, ?) WHERE attempt_id=?",
            (at, outcome, reason, cpu_seconds or 0.0, attempt_id),
        )

    def finish_attempt(
        self,
        attempt_id,
        outcome,
        reason,
        now=None,
        *,
        retryable=False,
        retry_delay=0.0,
        cpu_seconds=None,
        detail=None,
    ) -> str:
        """Close an open attempt without a result and move its task; returns the task state.

        failed/lost → `retry_wait` (after `retry_delay`) while retryable and fewer than
        `max_attempts` charged attempts exist; otherwise `failed`. A loss caused by
        coordinator shutdown is not charged and retries immediately. A pending
        cancellation always wins. canceled/quarantined are terminal.
        """
        if outcome not in ("failed", "lost", "canceled", "quarantined"):
            raise ValueError(f"finish_attempt cannot record {outcome!r}")
        at = epoch_seconds(now)
        with self.transaction():
            row = self._attempt_with_task(attempt_id)
            if row is None or row["finished_at"] is not None:
                raise StaleAttempt(f"attempt {attempt_id} is not open")
            task_id = row["task_id"]
            info = dict(detail or {}, n=row["n"], reason=reason)
            self._close(attempt_id, outcome, reason, at, cpu_seconds)
            if outcome in ("canceled", "quarantined"):
                _event(self.db, task_id, outcome, at, info, attempt_id)
                state = outcome
            else:
                charged = self.db.execute(
                    "SELECT COUNT(*) FROM attempts WHERE task_id=? AND NOT "
                    "(outcome IS 'lost' AND reason IS ?)",
                    (task_id, SHUTDOWN_REASON),
                ).fetchone()[0]
                shutdown = outcome == "lost" and reason == SHUTDOWN_REASON
                canceling = row["cancel_requested_at"] is not None
                final = canceling or not (shutdown or (retryable and charged < row["max_attempts"]))
                info.update(retryable=bool(retryable or shutdown), final=final)
                _event(self.db, task_id, outcome, at, info, attempt_id)
                if canceling:
                    _event(self.db, task_id, "canceled", at, {"reason": "cancel_requested"})
                    state = "canceled"
                elif final:
                    if outcome == "lost":
                        why = "retries_exhausted" if retryable else "not_retryable"
                        _event(self.db, task_id, "failed", at, {"reason": why, "final": True})
                    state = "failed"
                else:
                    not_before = at + (0.0 if shutdown else retry_delay)
                    _event(
                        self.db,
                        task_id,
                        "retry_scheduled",
                        at,
                        {"not_before": not_before, "charged_attempts": charged},
                    )
                    self.db.execute(
                        "UPDATE tasks SET state='retry_wait', not_before=? WHERE task_id=?",
                        (not_before, task_id),
                    )
                    return "retry_wait"
            self.db.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, task_id))
            return state

    def ingest_result(
        self, attempt_id, lease_token, manifest: ResultManifest, now=None, *, cpu_seconds=None
    ) -> str:
        """Record a succeeded/partial result: 'accepted', 'duplicate' or 'conflict'.

        Only the attempt's own lease token is accepted. A task has at most one accepted
        result: the same hash again is a no-op, a different hash is quarantined (recorded
        as a `quarantined` event carrying both hashes) and never replaces the result.
        Declared artifacts must already be in the artifact store.
        """
        if manifest.status not in ("succeeded", "partial"):
            raise ValueError("only succeeded or partial manifests carry results")
        record = manifest.record()
        result_hash = manifest.result_hash()
        at = epoch_seconds(now)
        for ref in manifest.artifacts:
            self.artifacts.verify(ref.sha256)
        with self.transaction():
            row = self._attempt_with_task(attempt_id)
            if (
                row is None
                or manifest.attempt_id != attempt_id
                or manifest.task_id != row["task_id"]
                or not hmac.compare_digest(row["lease_token"], lease_token)
                or manifest.lease_token != lease_token
            ):
                raise StaleAttempt(f"result does not belong to attempt {attempt_id}")
            task_id = row["task_id"]
            existing = self.db.execute(
                "SELECT result_hash FROM results WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] == result_hash:
                    return "duplicate"
                detail = {
                    "reason": "conflicting_result",
                    "accepted_hash": existing[0],
                    "offered_hash": result_hash,
                    "offered": record,
                }
                _event(self.db, task_id, "quarantined", at, detail, attempt_id)
                return "conflict"
            if row["finished_at"] is not None:
                raise StaleAttempt(f"attempt {attempt_id} is already finished")
            for ref in manifest.artifacts:
                self.db.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING",
                    (ref.sha256, ref.kind, ref.bytes, at, task_id),
                )
            self._close(attempt_id, manifest.status, "result", at, cpu_seconds)
            worker = {"pid": manifest.worker.pid, "env_names": list(manifest.worker.env_names)}
            worker["rlimits"] = manifest.worker.rlimits
            info = {"n": row["n"], "status": manifest.status, "worker": worker}
            _event(self.db, task_id, "succeeded", at, info, attempt_id)
            self.db.execute(
                "INSERT INTO results VALUES (?,?,?,?,?)",
                (task_id, attempt_id, result_hash, json_text(record), at),
            )
            detail = {"result_hash": result_hash, "status": manifest.status}
            _event(self.db, task_id, "ingested", at, detail, attempt_id)
            self.db.execute("UPDATE tasks SET state=? WHERE task_id=?", (manifest.status, task_id))
            return "accepted"


def open_store(root, *, readonly=False) -> OrchestrationStore:
    return OrchestrationStore(root, readonly=readonly)


# Worker-side lease functions: the only writes a worker performs ------------------------


def worker_connection(db_path) -> sqlite3.Connection:
    """Connection for a worker: never checkpoints (the coordinator does), NORMAL sync."""
    db = _connect(Path(db_path), synchronous="NORMAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        db.close()
        raise RuntimeError("unexpected orchestration schema version")
    return db


def claim_attempt(db, attempt_id, lease_token, pid, now, lease_seconds) -> dict | None:
    """Verify the token, record the worker PID once and extend the lease; None if refused."""
    with immediate(db):
        row = db.execute(
            "SELECT a.task_id, a.n, a.lease_token, a.worker_pid, a.finished_at, "
            "a.lease_expires_at, a.deadline_at, t.type, t.params_json, t.cancel_requested_at "
            "FROM attempts a JOIN tasks t USING(task_id) WHERE a.attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if (
            row is None
            or not hmac.compare_digest(row["lease_token"], lease_token)
            or row["finished_at"] is not None
            or row["worker_pid"] not in (None, pid)
            or row["lease_expires_at"] < now
            or row["cancel_requested_at"] is not None
        ):
            return None
        db.execute(
            "UPDATE attempts SET worker_pid=?, lease_expires_at=? WHERE attempt_id=?",
            (pid, now + lease_seconds, attempt_id),
        )
        _event(db, row["task_id"], "heartbeat", now, {"claimed": True, "pid": pid}, attempt_id)
        return {
            "task_id": row["task_id"],
            "n": row["n"],
            "type": row["type"],
            "params_json": row["params_json"],
            "deadline_at": row["deadline_at"],
            "lease_expires_at": now + lease_seconds,
        }


def renew_lease(db, attempt_id, lease_token, now, lease_seconds, cpu_seconds) -> bool:
    """Extend a live, unexpired, uncanceled lease; False tells the worker to stop itself."""
    with immediate(db):
        cursor = db.execute(
            "UPDATE attempts SET lease_expires_at=?, cpu_seconds=? WHERE attempt_id=? "
            "AND lease_token=? AND finished_at IS NULL AND lease_expires_at >= ? AND NOT EXISTS "
            "(SELECT 1 FROM tasks t WHERE t.task_id=attempts.task_id "
            "AND t.cancel_requested_at IS NOT NULL)",
            (now + lease_seconds, cpu_seconds, attempt_id, lease_token, now),
        )
        if cursor.rowcount != 1:
            return False
        task_id = attempt_id.rsplit("-", 1)[0]
        _event(db, task_id, "heartbeat", now, {"cpu_seconds": round(cpu_seconds, 3)}, attempt_id)
        return True


# Read-only summary ---------------------------------------------------------------------


def status(store: OrchestrationStore, now=None, *, recent=10) -> dict:
    """JSON-serializable operator summary; reads only (works on a readonly store)."""
    at = epoch_seconds(now)
    db = store.db
    by_state = dict(db.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state").fetchall())
    pods = []
    for pod_id, role in db.execute("SELECT pod_id, role FROM pods ORDER BY pod_id").fetchall():
        waiting = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE pod_id=? AND state IN ('queued','retry_wait')",
            (pod_id,),
        ).fetchone()[0]
        pods.append(
            {
                "pod_id": pod_id,
                "role": role,
                "budgets": store.budgets(pod_id),
                "usage": store.pod_usage(pod_id, at),
                "waiting_tasks": waiting,
            }
        )
    active = [
        {
            "attempt_id": row["attempt_id"],
            "task_id": row["task_id"],
            "pod_id": row["pod_id"],
            "type": row["type"],
            "n": row["n"],
            "worker_pid": row["worker_pid"],
            "started_at": iso(row["started_at"]),
            "lease_expires_at": iso(row["lease_expires_at"]),
            "deadline_at": iso(row["deadline_at"]),
            "cancel_requested": row["cancel_requested_at"] is not None,
            "log_path": row["log_path"],
        }
        for row in store.open_attempts()
    ]
    conflicts = db.execute(
        "SELECT COUNT(*) FROM task_events e JOIN tasks t USING(task_id) "
        "WHERE e.kind='quarantined' AND t.state <> 'quarantined'"
    ).fetchone()[0]
    failures = [
        {
            "task_id": row[0],
            "attempt_id": row[1],
            "kind": row[2],
            "at": iso(row[3]),
            "reason": row[4],
        }
        for row in db.execute(
            "SELECT task_id, attempt_id, kind, at, json_extract(detail_json, '$.reason') "
            "FROM task_events WHERE kind IN ('failed','lost','quarantined') "
            "ORDER BY id DESC LIMIT ?",
            (recent,),
        )
    ]
    candidates = dict(
        db.execute("SELECT state, COUNT(*) FROM candidates GROUP BY state").fetchall()
    )
    return {
        "generated_at": iso(at),
        "pods": pods,
        "tasks_by_state": by_state,
        "queued": by_state.get("queued", 0) + by_state.get("retry_wait", 0),
        "failed": by_state.get("failed", 0),
        "quarantined": by_state.get("quarantined", 0),
        "conflicting_results": conflicts,
        "active_attempts": active,
        "recent_failures": failures,
        "candidates_by_state": candidates,
        "holdouts_consumed": db.execute("SELECT COUNT(*) FROM holdout_ledger").fetchone()[0],
    }
