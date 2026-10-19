"""Shared harness for pod tests: temp orchestration roots, fast timings, orphan checks.

Every test that spawns workers goes through `Harness.coordinator()`; `close()` shuts each
coordinator down, kills any worker that is still alive (identity-checked) and fails the
test if one was found, so no test can leave an orphan process behind.
"""

import os
import signal
import time

from vader_intelligence.pods.contracts import (
    RESULT_SCHEMA,
    Budgets,
    ExperimentManifest,
    PodSpec,
    ResultManifest,
    TaskSpec,
    WorkerInfo,
)
from vader_intelligence.pods.coordinator import Coordinator, CoordinatorConfig, _identity
from vader_intelligence.pods.store import open_store
from vader_intelligence.provenance import source_hash

CODE_HASH = source_hash()
FAST = {
    "heartbeat_seconds": 0.1,
    "lease_seconds": 2.0,
    "cancel_grace_seconds": 0.3,
    "backoff_base_seconds": 0.05,
    "backoff_max_seconds": 0.2,
    "poll_seconds": 0.02,
    "kill_wait_seconds": 5.0,
}


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for(predicate, timeout=10.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not reached before timeout")


class Harness:
    def __init__(self, root):
        self.root = root
        self.store = open_store(root)
        self.coordinators = []

    def pod(self, pod_id="diag", role="diagnostics", **budgets):
        self.store.register_pod(PodSpec(pod_id, role, Budgets(**budgets)))
        return pod_id

    def manifest(self, task_type, params, seed=0):
        manifest = ExperimentManifest(
            dataset_manifest_hash="0" * 64,
            feature_set_versions={"market_features": "v1"},
            split={"development": {"start": "2026-04-01", "end": "2026-07-31"}},
            code_hash=CODE_HASH,
            operation=task_type,
            params=params,
            seed=seed,
            budget={"max_seconds": 60},
        )
        return self.store.put_experiment_manifest(manifest)

    def task(self, task_type, params=None, *, pod="diag", priority=0, max_attempts=3, seed=0):
        params = params or {}
        manifest = self.manifest(task_type, params, seed)
        return TaskSpec(pod, task_type, params, manifest, priority, max_attempts)

    def submit(self, task_type, params=None, **kwargs):
        return self.store.submit(self.task(task_type, params, **kwargs))

    def coordinator(self, **overrides):
        coordinator = Coordinator(self.store, CoordinatorConfig(**{**FAST, **overrides}))
        self.coordinators.append(coordinator)
        return coordinator

    def state(self, task_id):
        return self.store.task(task_id)["state"]

    def attempts(self, task_id):
        return self.store.attempts(task_id)

    def kinds(self, task_id):
        return [e["kind"] for e in self.store.events(task_id) if e["kind"] != "heartbeat"]

    def until(self, coordinator, predicate, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            coordinator.step()
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("coordinator did not reach the condition before timeout")

    def claimed_pid(self, task_id, n=1, timeout=10.0):
        def claimed():
            rows = self.attempts(task_id)
            return len(rows) >= n and rows[n - 1]["worker_pid"] is not None

        wait_for(claimed, timeout)
        return self.attempts(task_id)[n - 1]["worker_pid"]

    def accept(self, task_id, output=None):
        """Store-level acceptance of a result without spawning a process."""
        now = time.time()
        attempt = self.store.create_attempt(
            task_id, now, lease_seconds=30, deadline_at=now + 60, limits={"cpu_seconds": 1}
        )
        manifest = ResultManifest(
            schema=RESULT_SCHEMA,
            task_id=task_id,
            attempt_id=attempt["attempt_id"],
            lease_token=attempt["lease_token"],
            status="succeeded",
            output=output if output is not None else {"ok": True},
            error=None,
            artifacts=(),
            worker=WorkerInfo(pid=1, env_names=(), rlimits={}, cpu_seconds=0.0),
        )
        verdict = self.store.ingest_result(
            attempt["attempt_id"], attempt["lease_token"], manifest, now
        )
        return attempt, manifest, verdict

    def live_workers(self):
        rows = self.store.db.execute(
            "SELECT attempt_id, worker_pid FROM attempts WHERE worker_pid IS NOT NULL"
        ).fetchall()
        return [(aid, pid) for aid, pid in rows if pid_alive(pid) and _identity(pid, aid)]

    def close(self):
        try:
            for coordinator in self.coordinators:
                coordinator.shutdown()
            leftovers = self.live_workers()
            for _, pid in leftovers:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            assert not leftovers, f"orphan workers after shutdown: {leftovers}"
        finally:
            self.store.close()
