"""Coordinator with real worker subprocesses (no network, no tmux).

Each test uses a temporary orchestration root and short timings; the `pods` fixture
shuts every coordinator down and fails if any worker process survives.
"""

import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vader_intelligence.pods.contracts import ContractError, parse_result_manifest
from vader_intelligence.pods.coordinator import (
    ENV_ALLOWLIST,
    CoordinatorLocked,
    worker_environment,
)
from vader_intelligence.pods.store import DAY_SECONDS, SHUTDOWN_REASON, status

from ._harness import FAST, pid_alive, wait_for

# Names the OS or interpreter adds on its own: PEP 538 locale coercion and macOS CF.
RUNTIME_ADDED = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}


def outcomes(pods, task_id):
    return [(a["outcome"], a["reason"]) for a in pods.attempts(task_id)]


def test_worker_runs_with_scrubbed_environment_and_limits(pods, monkeypatch, tmp_path):
    secret = tmp_path / "live-secret.pem"
    secret.write_text("not a real key")
    monkeypatch.setenv("KALSHI_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("VI_LIVE_SECRET_PATH", str(secret))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    pods.pod()
    task_id = pods.submit("noop")
    coordinator = pods.coordinator()
    assert coordinator.run(max_steps=200, deadline_seconds=10)["stopped"] == "idle"
    assert pods.state(task_id) == "succeeded"
    (attempt,) = pods.attempts(task_id)
    succeeded = next(e for e in pods.store.events(task_id) if e["kind"] == "succeeded")
    names = set(succeeded["detail"]["worker"]["env_names"])
    assert not names & {"KALSHI_API_KEY", "VI_LIVE_SECRET_PATH", "PYTHONPATH"}
    assert {"HOME", "PATH", "PYTHONHASHSEED", "TZ", "VI_TASK_ATTEMPT_ID"} <= names
    assert all(n in ENV_ALLOWLIST or n.startswith("VI_TASK_") or n in RUNTIME_ADDED for n in names)
    env = worker_environment(attempt, pods.store.path, coordinator.config)
    assert env["HOME"] == attempt["workdir"] and env["PYTHONHASHSEED"] == "0"
    assert str(secret) not in "".join(env.values())
    limits = succeeded["detail"]["worker"]["rlimits"]
    assert limits["cpu"][0] == attempt["cpu_limit_seconds"] == 600
    assert limits["nofile"] == [256, 256] and limits["fsize"] == [1 << 30, 1 << 30]
    # Re-ingesting the worker's own manifest is a no-op.
    manifest = parse_result_manifest((Path(attempt["workdir"]) / "result.json").read_bytes())
    token = attempt["lease_token"]
    assert pods.store.ingest_result(attempt["attempt_id"], token, manifest) == "duplicate"


def test_kill_9_mid_task_then_retry_to_success(pods):
    pods.pod()
    task_id = pods.submit("sleep", {"seconds": 0.5}, max_attempts=3)
    coordinator = pods.coordinator()
    coordinator.step()
    pid = pods.claimed_pid(task_id)
    os.kill(pid, signal.SIGKILL)
    assert pods.submit("sleep", {"seconds": 0.5}, max_attempts=3) == task_id  # idempotent
    pods.until(coordinator, lambda: pods.state(task_id) == "succeeded")
    assert outcomes(pods, task_id) == [("lost", "signal_9"), ("succeeded", "result")]
    assert "retry_scheduled" in pods.kinds(task_id)
    assert not pid_alive(pid)


def test_crash_operation_is_lost_then_retried(pods):
    pods.pod()
    task_id = pods.submit("crash", {"exit_code": 70, "crash_attempts": 1})
    coordinator = pods.coordinator()
    coordinator.run(max_steps=500, deadline_seconds=15)
    assert pods.state(task_id) == "succeeded"
    assert outcomes(pods, task_id) == [("lost", "exit_70"), ("succeeded", "result")]
    assert pods.store.result(task_id)["result"]["output"] == {"survived_on_attempt": 2}


def test_lease_expiry_reaps_a_silent_worker(pods):
    pods.pod()
    task_id = pods.submit("sleep", {"seconds": 0.3})
    coordinator = pods.coordinator(lease_seconds=1.0, heartbeat_seconds=0.2)
    coordinator.step()
    pid = pods.claimed_pid(task_id)
    os.kill(pid, signal.SIGSTOP)  # alive but silent: heartbeats stop
    pods.until(coordinator, lambda: len(pods.attempts(task_id)) == 2)
    first = pods.attempts(task_id)[0]
    assert (first["outcome"], first["reason"]) == ("lost", "lease_expired")
    wait_for(lambda: not pid_alive(pid))
    pods.until(coordinator, lambda: pods.state(task_id) == "succeeded")


def test_heartbeats_renew_the_lease(pods):
    pods.pod()
    task_id = pods.submit("sleep", {"seconds": 1.5})
    coordinator = pods.coordinator(lease_seconds=0.6, heartbeat_seconds=0.1)
    coordinator.run(max_steps=1000, deadline_seconds=15)
    assert pods.state(task_id) == "succeeded" and len(pods.attempts(task_id)) == 1
    beats = [e for e in pods.store.events(task_id) if e["kind"] == "heartbeat"]
    assert len(beats) >= 5 and beats[0]["detail"]["claimed"] is True
    attempt = pods.attempts(task_id)[0]
    assert attempt["lease_expires_at"] - attempt["started_at"] > 1.5
    assert attempt["cpu_seconds"] > 0


def test_wall_deadline_kills_the_worker(pods):
    pods.pod(wall_seconds_per_task=0.5)
    task_id = pods.submit("sleep", {"seconds": 30}, max_attempts=1)
    coordinator = pods.coordinator()
    started = time.monotonic()
    coordinator.step()
    pid = pods.claimed_pid(task_id)
    pods.until(coordinator, lambda: pods.state(task_id) == "failed")
    assert time.monotonic() - started < 10
    assert outcomes(pods, task_id) == [("failed", "wall_deadline")]
    assert not pid_alive(pid)


def test_retries_stop_at_max_attempts_and_transient_failures_recover(pods):
    pods.pod(max_concurrent=2)
    hopeless = pods.submit("fail", {"mode": "transient", "fail_attempts": 1000}, max_attempts=3)
    recovers = pods.submit("fail", {"mode": "transient", "fail_attempts": 1}, max_attempts=3)
    pods.coordinator().run(max_steps=1000, deadline_seconds=20)
    assert pods.state(hopeless) == "failed"
    assert outcomes(pods, hopeless) == [("failed", "TransientFailure")] * 3
    assert pods.kinds(hopeless).count("retry_scheduled") == 2
    assert pods.state(recovers) == "succeeded" and len(pods.attempts(recovers)) == 2


def test_deterministic_failures_are_not_retried(pods):
    pods.pod()
    pods.pod("baseline", "baseline_models")
    failing = pods.submit("fail", {"mode": "deterministic"}, max_attempts=5)
    params = {
        "dataset_manifest_hash": "0" * 64,
        "feature_set": "market",
        "feature_set_version": "v1",
        "model_family": "logistic",
        "seed": 0,
    }
    with pytest.raises(ContractError, match="not stored"):
        pods.submit("train_model", params, pod="baseline")  # its input snapshot is missing
    params["dataset_manifest_hash"] = pods.store.put_input(b"not a snapshot", "dataset_snapshot")
    refused = pods.submit("train_model", params, pod="baseline", max_attempts=5)
    pods.coordinator().run(max_steps=500, deadline_seconds=15)
    assert outcomes(pods, failing) == [("failed", "DeterministicFailure")]
    assert outcomes(pods, refused) == [("failed", "DeterministicFailure")]
    assert pods.state(failing) == pods.state(refused) == "failed"


def test_concurrency_budget(pods):
    pods.pod(max_concurrent=2)
    tasks = [pods.submit("sleep", {"seconds": 0.4}, seed=i) for i in range(4)]
    coordinator = pods.coordinator()
    assert len(coordinator.step()["spawned"]) == 2
    peak = 0
    while any(pods.state(t) != "succeeded" for t in tasks):
        report = coordinator.step()
        peak = max(peak, report["active"])
        assert len(status(pods.store)["active_attempts"]) <= 2
        time.sleep(0.02)
    assert peak == 2


def test_daily_task_budget(pods):
    pods.pod(tasks_per_day=2, max_concurrent=3)
    tasks = [pods.submit("noop", seed=i) for i in range(3)]
    coordinator = pods.coordinator()
    assert coordinator.run(max_steps=200, deadline_seconds=10)["stopped"] == "idle"
    assert sorted(pods.state(t) for t in tasks) == ["queued", "succeeded", "succeeded"]
    tomorrow = (math.floor(time.time() / DAY_SECONDS) + 1) * DAY_SECONDS + 1
    assert len(coordinator.step(tomorrow)["spawned"]) == 1
    coordinator.run(max_steps=200, deadline_seconds=10)
    assert [pods.state(t) for t in tasks] == ["succeeded"] * 3


def test_daily_cpu_budget(pods):
    pods.pod(cpu_seconds_per_day=0.01, max_concurrent=1)
    tasks = [pods.submit("noop", seed=i) for i in range(2)]
    coordinator = pods.coordinator()
    coordinator.run(max_steps=200, deadline_seconds=10)
    done = [t for t in tasks if pods.state(t) == "succeeded"]
    assert len(done) == 1  # the second waits once the day's CPU is spent
    (attempt,) = pods.attempts(done[0])
    assert attempt["cpu_seconds"] >= 0.01 and attempt["cpu_limit_seconds"] == 1
    tomorrow = (math.floor(time.time() / DAY_SECONDS) + 1) * DAY_SECONDS + 1
    assert len(coordinator.step(tomorrow)["spawned"]) == 1
    coordinator.run(max_steps=200, deadline_seconds=10)
    assert [pods.state(t) for t in tasks] == ["succeeded"] * 2


def test_cancellation_kills_the_process_group(pods):
    pods.pod()
    task_id = pods.submit("sleep", {"seconds": 30, "with_child": True})
    coordinator = pods.coordinator()
    coordinator.step()
    pid = pods.claimed_pid(task_id)
    workdir = Path(pods.attempts(task_id)[0]["workdir"])
    wait_for(lambda: (workdir / "child.pid").exists() and (workdir / "child.pid").read_text())
    child = int((workdir / "child.pid").read_text())
    assert pid_alive(child) and os.getpgid(child) == pid
    started = time.monotonic()
    assert coordinator.cancel(task_id) == "leased"
    pods.until(coordinator, lambda: pods.state(task_id) == "canceled")
    assert time.monotonic() - started < 5
    assert outcomes(pods, task_id) == [("canceled", "cancel_requested")]
    wait_for(lambda: not pid_alive(child) and not pid_alive(pid))
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


@pytest.mark.parametrize(
    "tamper,reason",
    [
        (lambda raw: b"{not json", "malformed_manifest"),
        (
            lambda raw: raw.replace(b'"status":"succeeded"', b'"status":"done"'),
            "malformed_manifest",
        ),
        (lambda raw: raw.replace(b'"lease_token":"', b'"lease_token":"0'), "malformed_manifest"),
        (lambda raw: _swap_token(raw), "lease_token_mismatch"),
    ],
)
def test_bad_manifests_are_quarantined(pods, tamper, reason):
    pods.pod()
    task_id = pods.submit("sleep", {"seconds": 0.1})
    coordinator = pods.coordinator()
    coordinator.step()
    path = Path(pods.attempts(task_id)[0]["workdir"]) / "result.json"
    wait_for(path.exists)
    path.write_bytes(tamper(path.read_bytes()))
    pods.until(coordinator, lambda: pods.state(task_id) == "quarantined")
    assert outcomes(pods, task_id) == [("quarantined", reason)]
    assert pods.store.result(task_id) is None
    assert status(pods.store)["quarantined"] == 1


def _swap_token(raw):
    start = raw.index(b'"lease_token":"') + len(b'"lease_token":"')
    return raw[:start] + b"0" * 32 + raw[start + 32 :]


def test_artifacts_are_verified_and_a_hash_mismatch_is_quarantined(pods):
    pods.pod()
    good = pods.submit("emit_artifact", {"name": "model.json", "text": '{"w": 1}'})
    coordinator = pods.coordinator()
    coordinator.run(max_steps=200, deadline_seconds=10)
    assert pods.state(good) == "succeeded"
    (ref,) = pods.store.result(good)["result"]["artifacts"]
    assert pods.store.artifacts.get_bytes(ref["sha256"]) == b'{"w": 1}'
    tampered = pods.submit("emit_artifact", {"name": "m.json", "text": "original"})
    coordinator.step()
    workdir = Path(pods.attempts(tampered)[0]["workdir"])
    wait_for((workdir / "result.json").exists)
    (workdir / "out" / "m.json").write_text("modified")  # same size, different bytes
    pods.until(coordinator, lambda: pods.state(tampered) == "quarantined")
    assert outcomes(pods, tampered) == [("quarantined", "artifact_hash_mismatch")]
    declared = parse_result_manifest((workdir / "result.json").read_bytes()).artifacts[0]
    assert not pods.store.artifacts.has(declared.sha256)


def test_bounded_run_and_no_orphans_after_shutdown(pods):
    pods.pod(max_concurrent=3)
    tasks = [pods.submit("sleep", {"seconds": 30}, seed=i) for i in range(3)]
    coordinator = pods.coordinator()
    started = time.monotonic()
    assert coordinator.run(max_steps=10_000, deadline_seconds=0.5)["stopped"] == "deadline"
    assert time.monotonic() - started < 2
    assert coordinator.run(max_steps=3, deadline_seconds=30) == {"steps": 3, "stopped": "max_steps"}
    pids = [pods.claimed_pid(t) for t in tasks]
    assert all(pid_alive(pid) for pid in pids)
    report = coordinator.shutdown()
    assert sorted(report["killed"]) == sorted(
        a["attempt_id"] for t in tasks for a in pods.attempts(t)
    )
    assert report["unreaped"] == []
    assert not any(pid_alive(pid) for pid in pids)
    for task_id in tasks:
        assert outcomes(pods, task_id) == [("lost", SHUTDOWN_REASON)]
        assert pods.state(task_id) == "retry_wait"  # shutdown losses are not charged
    with pytest.raises(RuntimeError):
        coordinator.step()
    pods.coordinator().shutdown()  # the lock was released


def test_only_one_coordinator_per_root(pods):
    pods.pod()
    pods.coordinator()
    with pytest.raises(CoordinatorLocked):
        pods.coordinator()


def test_coordinator_restart_recovers_running_attempt_from_the_database(pods):
    pods.pod()
    task_id = pods.submit("sleep", {"seconds": 1.5})
    script = (
        "import time\n"
        "from vader_intelligence.pods.coordinator import Coordinator, CoordinatorConfig\n"
        "from vader_intelligence.pods.store import open_store\n"
        f"coordinator = Coordinator(open_store({str(pods.root)!r}), CoordinatorConfig(**{FAST!r}))\n"
        "coordinator.step()\n"
        "print('spawned', flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout.readline().strip() == "spawned"
        pid = pods.claimed_pid(task_id)
        parent.kill()  # the first coordinator dies without shutdown
        parent.wait(timeout=5)
        assert pid_alive(pid)  # its worker keeps running and heartbeating
        coordinator = pods.coordinator()  # recovers from the database alone
        assert coordinator.step()["active"] == 1
        coordinator.run(max_steps=1000, deadline_seconds=15)
        assert pods.state(task_id) == "succeeded"
        assert outcomes(pods, task_id) == [("succeeded", "result")]
        assert not pid_alive(pid)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        parent.stdout.close()


def test_shutdown_kills_workers_spawned_by_a_dead_coordinator(pods):
    pods.pod()
    task_id = pods.submit("sleep", {"seconds": 30})
    script = (
        "import time\n"
        "from vader_intelligence.pods.coordinator import Coordinator, CoordinatorConfig\n"
        "from vader_intelligence.pods.store import open_store\n"
        f"coordinator = Coordinator(open_store({str(pods.root)!r}), CoordinatorConfig(**{FAST!r}))\n"
        "coordinator.step()\n"
        "print('spawned', flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout.readline().strip() == "spawned"
        pid = pods.claimed_pid(task_id)
        parent.kill()
        parent.wait(timeout=5)
        report = pods.coordinator().shutdown()
        assert report["killed"] == [pods.attempts(task_id)[0]["attempt_id"]]
        wait_for(lambda: not pid_alive(pid))
        assert outcomes(pods, task_id) == [("lost", SHUTDOWN_REASON)]
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        parent.stdout.close()
