"""One bounded collector invocation and evidence-based operational health."""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ops.archive import classify
from ops.common import adjacent, command, digest, environment, lock, read_state, write_json


def utc():
    return datetime.now(timezone.utc).isoformat()


def gap(previous, current, cadence, grace):
    seconds = current - previous
    if seconds < 0:
        return {"kind": "clock_regression", "seconds": seconds, "cause": "unknown"}
    if seconds > cadence + grace:
        return {
            "kind": "collection_gap",
            "from_epoch": previous + cadence,
            "to_epoch": current,
            "seconds": seconds - cadence,
            "cause": "unknown",
        }
    return None


def terminate(process, grace):
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
    else:
        os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=grace)


def capture(pipe, path, limit, outcome):
    try:
        with pipe, open(path, "wb") as stream:
            count = 0
            while chunk := pipe.read(8192):
                keep = chunk[: max(0, limit - count)]
                stream.write(keep)
                count += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
            outcome["truncated"] = count > limit
    except OSError as exc:
        outcome["error"] = str(exc)


def supervise(config, argv, stdout, stderr):
    interrupted = threading.Event()
    handlers = {}
    process = None
    threads = []
    captures = [{}, {}]
    try:
        # Only set an event in the handler; process control happens in the main loop.
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: interrupted.set())
        process = subprocess.Popen(
            argv,
            cwd=config["checkout"],
            env=environment(config),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        for pipe, path, outcome in zip(
            (process.stdout, process.stderr), (stdout, stderr), captures
        ):
            thread = threading.Thread(
                target=capture,
                args=(pipe, path, config["log_bytes"], outcome),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        deadline = time.monotonic() + config["deadline"]
        reason = None
        while process.poll() is None:
            if interrupted.is_set():
                reason = "interrupted"
                break
            if time.monotonic() >= deadline:
                reason = "deadline_exceeded"
                break
            if any("error" in output for output in captures):
                reason = "log_write_failed"
                break
            time.sleep(0.05)
        if reason:
            terminate(process, config["grace"])
        code = process.wait()
        for thread in threads:
            thread.join(timeout=config["grace"])
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("collector output pipe did not close")
        if any("error" in output for output in captures):
            raise OSError("collector log write failed")
        if any(output.get("truncated") for output in captures):
            reason = reason or "output_limit_exceeded"
        return code, reason
    finally:
        if process is not None and process.poll() is None:
            terminate(process, config["grace"])
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def run(config):
    try:
        with lock(adjacent(config, "run.lockfile")):
            return run_locked(config)
    except BlockingIOError:
        now = utc()
        result = {
            "status": "overlap_skipped",
            "started_at": now,
            "ended_at": now,
            "runtime_seconds": 0,
            "exit_code": 75,
        }
        # Separate fixed-size evidence, so an overlap cannot clobber the active history.
        with lock(adjacent(config, "overlap.lockfile")):
            write_json(Path(config["state"]) / "last-overlap.json", result)
        return result, 75


def run_locked(config):
    data = read_state(config)
    history = data["history"]
    started, epoch, mono = utc(), time.time(), time.monotonic()
    sequence = data["sequence"] + 1
    slot = sequence % config["retention"]
    state = Path(config["state"])
    stdout, stderr = state / f"stdout-{slot}.log", state / f"stderr-{slot}.log"
    entry = {
        "sequence": sequence,
        "started_at": started,
        "start_epoch": epoch,
        "ended_at": None,
        "status": "running",
        "argv": command(config),
        "stdout": str(stdout),
        "stderr": str(stderr),
        "pid": os.getpid(),
    }
    if history:
        previous = history[-1]
        entry["gap_before"] = gap(
            previous["start_epoch"],
            epoch,
            config["cadence"],
            config["grace"],
        )
        if previous["ended_at"] is None:
            previous["status"] = "completion_unknown"
    history.append(entry)
    data.update(sequence=sequence, history=history[-config["retention"] :])
    write_json(state / "history.json", data)
    exit_code = 1
    try:
        entry["collector_config_sha256"] = digest(config["collector_config"])
        code, reason = supervise(config, entry["argv"], stdout, stderr)
        entry["collector_exit_code"] = code
        if reason:
            entry["status"] = reason
            exit_code = (
                124 if reason == "deadline_exceeded" else 130 if reason == "interrupted" else 1
            )
        else:
            result = json.loads(stdout.read_text(encoding="utf-8"))
            entry["status"] = classify(config, result, code)
            entry["run_id"] = result.get("run_id") if isinstance(result, dict) else None
            entry["collector_status"] = result.get("status") if isinstance(result, dict) else None
            entry["errors"] = (
                str(result.get("errors", []))[:1000] if isinstance(result, dict) else ""
            )
            exit_code = code if code in (0, 1, 3, 130) else 1
            if entry["status"] not in {"collected_eligible_games", "discovery_no_eligible_games"}:
                exit_code = exit_code or 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        entry.update(status="wrapper_failed", error=str(exc)[:1000])
    finally:
        entry.update(
            ended_at=utc(),
            end_epoch=time.time(),
            runtime_seconds=time.monotonic() - mono,
            exit_code=exit_code,
        )
        write_json(state / "history.json", data)
    return entry, exit_code


def health(config, *, now=None):
    data = read_state(config)
    history = data["history"]
    now = time.time() if now is None else now
    latest = history[-1] if history else None
    status = "never_invoked"
    current_gap = None
    if latest:
        age = now - latest["start_epoch"]
        current_gap = gap(latest["start_epoch"], now, config["cadence"], config["grace"])
        if age < 0:
            status = "clock_regression"
        elif current_gap:
            status = "missed_or_stale"
        elif latest["ended_at"] is None:
            status = "running" if age <= config["deadline"] + config["grace"] else "stale_activity"
        else:
            status = latest["status"]
    db = Path(config["database"])
    free = shutil.disk_usage(db.parent).free
    return {
        "status": status,
        "latest": latest,
        "current_gap": current_gap,
        "recorded_gaps": [e["gap_before"] for e in history if e.get("gap_before")],
        "retained_invocations": len(history),
        "history_path": str(Path(config["state"]) / "history.json"),
        "archive_bytes": {
            str(p): p.stat().st_size if p.exists() else 0
            for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm"))
        },
        "free_bytes": free,
        "storage_pressure": free < 512 * 1024 * 1024,
        "gap_cause": "unknown; inspect power and collector request evidence before attributing",
    }
