"""Orchestration store: idempotency, ingestion, retry policy, triggers, leases, status.

Store-level only: attempts are created and finished directly, no processes are spawned.
"""

import json
import re
import sqlite3
import time

import pytest

from vader_intelligence.pods.contracts import (
    ATTEMPT_OUTCOMES,
    BUDGET_WINDOWS,
    EVENT_KINDS,
    ROLES,
    TASK_STATES,
    Budgets,
    ContractError,
    PodSpec,
    TaskSpec,
)
from vader_intelligence.pods.store import (
    SHUTDOWN_REASON,
    IdempotencyConflict,
    StaleAttempt,
    claim_attempt,
    open_store,
    renew_lease,
    status,
    worker_connection,
)


def lease(pods, task_id, now=None):
    now = time.time() if now is None else now
    return pods.store.create_attempt(
        task_id, now, lease_seconds=30, deadline_at=now + 60, limits={"cpu_seconds": 1}
    )


def test_submission_is_idempotent_and_validated(pods):
    pods.pod()
    pods.pod("baseline", "baseline_models")
    first = pods.submit("sleep", {"seconds": 1})
    again = pods.submit("sleep", {"seconds": 1.0})
    assert first == again
    assert pods.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert pods.kinds(first) == ["submitted"]
    pods.pod("diag2")
    with pytest.raises(IdempotencyConflict):
        pods.submit("sleep", {"seconds": 1}, pod="diag2")
    with pytest.raises(ContractError):
        pods.submit("noop", pod="baseline")  # role does not permit test operations
    with pytest.raises(ContractError):
        pods.submit("noop", pod="nopod")
    other = pods.manifest("sleep", {"seconds": 2})
    with pytest.raises(ContractError):
        pods.store.submit(TaskSpec("diag", "sleep", {"seconds": 1}, other))
    with pytest.raises(ContractError):
        pods.store.submit(TaskSpec("diag", "noop", {}, "9" * 64))
    with pytest.raises(IdempotencyConflict):
        pods.store.register_pod(PodSpec("diag", "diagnostics", Budgets(max_concurrent=5)))
    assert pods.store.register_pod(PodSpec("baseline", "baseline_models")) is False


def test_result_ingestion_is_idempotent_and_conflicts_are_quarantined(pods):
    pods.pod()
    task_id = pods.submit("noop")
    attempt, manifest, verdict = pods.accept(task_id, {"value": 1})
    assert verdict == "accepted" and pods.state(task_id) == "succeeded"
    stored = pods.store.result(task_id)
    assert stored["result_hash"] == manifest.result_hash()
    token = attempt["lease_token"]
    assert pods.store.ingest_result(attempt["attempt_id"], token, manifest) == "duplicate"
    conflicting = manifest.__class__(**{**manifest.__dict__, "output": {"value": 2}})
    assert pods.store.ingest_result(attempt["attempt_id"], token, conflicting) == "conflict"
    assert pods.store.result(task_id) == stored  # never replaced
    event = pods.store.events(task_id)[-1]
    assert event["kind"] == "quarantined" and event["detail"]["reason"] == "conflicting_result"
    assert event["detail"]["accepted_hash"] == manifest.result_hash()
    assert event["detail"]["offered_hash"] == conflicting.result_hash()
    assert pods.state(task_id) == "succeeded"  # terminal states are absorbing
    assert status(pods.store)["conflicting_results"] == 1
    with pytest.raises(StaleAttempt):
        pods.store.ingest_result(attempt["attempt_id"], "0" * 32, manifest)


def test_retry_policy_charges_attempts_except_shutdown_losses(pods):
    pods.pod()
    task_id = pods.submit("noop", max_attempts=2)
    now = 1_000_000.0
    first = lease(pods, task_id, now)
    state = pods.store.finish_attempt(
        first["attempt_id"], "lost", "lease_expired", now, retryable=True, retry_delay=5
    )
    assert state == "retry_wait"
    assert pods.store.schedulable_tasks(now + 1) == []
    assert [t["task_id"] for t in pods.store.schedulable_tasks(now + 5)] == [task_id]
    second = lease(pods, task_id, now + 5)
    assert pods.store.finish_attempt(second["attempt_id"], "lost", SHUTDOWN_REASON, now + 6) == (
        "retry_wait"
    )
    third = lease(pods, task_id, now + 6)  # shutdown loss is free and retries at once
    state = pods.store.finish_attempt(
        third["attempt_id"], "lost", "lease_expired", now + 7, retryable=True
    )
    assert state == "failed"
    assert pods.kinds(task_id)[-2:] == ["lost", "failed"]
    with pytest.raises(StaleAttempt):
        pods.store.finish_attempt(third["attempt_id"], "lost", "again", now + 8)
    deterministic = pods.submit("fail", {"mode": "deterministic"}, max_attempts=5)
    attempt = lease(pods, deterministic, now)
    state = pods.store.finish_attempt(attempt["attempt_id"], "failed", "DeterministicFailure", now)
    assert state == "failed" and len(pods.attempts(deterministic)) == 1


def test_cancellation_of_waiting_and_leased_tasks(pods):
    pods.pod()
    waiting = pods.submit("noop")
    assert pods.store.request_cancel(waiting) == "canceled"
    assert pods.store.request_cancel(waiting) == "canceled"
    assert pods.kinds(waiting) == ["submitted", "cancel_requested", "canceled"]
    leased = pods.submit("sleep", {"seconds": 1})
    attempt = lease(pods, leased)
    assert pods.store.request_cancel(leased) == "leased"
    assert pods.store.schedulable_tasks() == []
    state = pods.store.finish_attempt(
        attempt["attempt_id"], "lost", "lease_expired", retryable=True
    )
    assert state == "canceled"  # a pending cancellation always wins over a retry


def test_schema_enforces_append_only_history_and_legal_state_changes(pods):
    pods.pod()
    task_id = pods.submit("noop")
    pods.accept(task_id)
    db = pods.store.db
    for statement in (
        "UPDATE task_events SET kind='failed'",
        "DELETE FROM task_events",
        "UPDATE results SET result_hash=result_hash",
        "DELETE FROM results",
        "UPDATE artifacts SET bytes=0",
        "DELETE FROM artifacts",
        "UPDATE pods SET role='diagnostics'",
        "DELETE FROM budgets",
        "DELETE FROM attempts",
        "DELETE FROM tasks",
        "UPDATE attempts SET outcome='failed'",
        f"UPDATE tasks SET state='queued' WHERE task_id='{task_id}'",
        f"UPDATE tasks SET params_json='{{}}' WHERE task_id='{task_id}'",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(statement)
    other = pods.submit("sleep", {"seconds": 1})
    with pytest.raises(sqlite3.DatabaseError, match="requires its event"):
        db.execute(f"UPDATE tasks SET state='leased' WHERE task_id='{other}'")
    db.execute(  # even with a justifying event, queued -> succeeded is not an edge
        "INSERT INTO task_events(task_id, kind, at, detail_json) VALUES (?, 'ingested', 0, '{}')",
        (other,),
    )
    with pytest.raises(sqlite3.DatabaseError, match="illegal task state"):
        db.execute(f"UPDATE tasks SET state='succeeded' WHERE task_id='{other}'")


def test_worker_lease_functions_require_the_token(pods):
    pods.pod()
    task_id = pods.submit("sleep", {"seconds": 1})
    attempt = lease(pods, task_id)
    db = worker_connection(pods.store.path)
    try:
        aid, token, now = attempt["attempt_id"], attempt["lease_token"], time.time()
        assert claim_attempt(db, aid, "0" * 32, 100, now, 5) is None
        claim = claim_attempt(db, aid, token, 100, now, 5)
        assert claim["type"] == "sleep" and json.loads(claim["params_json"])["seconds"] == 1.0
        assert claim_attempt(db, aid, token, 101, now, 5) is None  # pid recorded once
        assert renew_lease(db, aid, token, now + 1, 5, 0.5) is True
        assert renew_lease(db, aid, "0" * 32, now + 1, 5, 0.5) is False
        assert renew_lease(db, aid, token, now + 100, 5, 0.5) is False  # lapsed lease
        assert pods.attempts(task_id)[0]["worker_pid"] == 100
        pods.store.request_cancel(task_id)
        assert renew_lease(db, aid, token, now + 2, 5, 0.5) is False  # canceled
    finally:
        db.close()


def test_status_is_json_serializable_and_works_read_only(pods):
    pods.pod(max_concurrent=2)
    task_id = pods.submit("sleep", {"seconds": 1})
    lease(pods, task_id)
    pods.submit("noop")
    summary = status(pods.store)
    json.dumps(summary)
    assert summary["queued"] == 1 and len(summary["active_attempts"]) == 1
    assert summary["pods"][0]["budgets"]["max_concurrent"] == 2
    readonly = open_store(pods.root, readonly=True)
    try:
        assert status(readonly)["tasks_by_state"] == {"leased": 1, "queued": 1}
        with pytest.raises(sqlite3.DatabaseError):
            readonly.db.execute("DELETE FROM task_events")
    finally:
        readonly.close()


def test_schema_vocabularies_match_the_contracts(pods):
    def allowed(table, column):
        sql = pods.store.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        found = re.search(rf"{column} TEXT[^,]*?CHECK\({column} IN\s*\(([^)]*)\)\)", sql)
        return {value.strip().strip("'") for value in found.group(1).split(",")}

    assert allowed("tasks", "state") == set(TASK_STATES)
    assert allowed("task_events", "kind") == set(EVENT_KINDS)
    assert allowed("attempts", "outcome") == set(ATTEMPT_OUTCOMES)
    assert allowed("pods", "role") == set(ROLES)
    assert allowed("budgets", "kind") == set(BUDGET_WINDOWS)
