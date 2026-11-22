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
from ops.common import (
    adjacent,
    command,
    digest,
    environment,
    lock,
    maintenance_command,
    read_state,
    tick_command,
    write_json,
)

TICK_STATUSES = {"complete", "inconclusive", "partial", "failed"}


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
        signal_group(process, signal.SIGINT)
    try:
        process.wait(timeout=grace / 2)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            signal_group(process, signal.SIGKILL)
        process.wait(timeout=grace / 2)


def signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass  # The process group already exited during the poll/signal race.


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
        code = process.wait(timeout=config["grace"])
        # Reap descendants even if their leader has exited; otherwise inherited
        # pipes (and potentially writer locks) can outlive the scheduled wrapper.
        if os.name != "nt":
            signal_group(process, signal.SIGKILL)
        cleanup_end = time.monotonic() + config["grace"]
        for thread in threads:
            thread.join(timeout=max(0, cleanup_end - time.monotonic()))
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
        if process is not None and os.name != "nt":
            signal_group(process, signal.SIGKILL)
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
        if "shadow" in config:
            # The archive-only forecast tick runs first, serialized in this invocation,
            # with its own bounded deadline; its status is recorded separately from the
            # collection that follows (specification §7: tick before collection).
            entry["tick"] = run_tick(config, slot, state)
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
        if entry.get("tick") and entry["tick"]["status"] in (
            "failed",
            "deadline_exceeded",
            "interrupted",
            "invalid_output",
            "log_write_failed",
            "wrapper_failed",
            "output_limit_exceeded",
        ):
            # An operational tick failure is never hidden behind a healthy collection
            # exit code; collection status itself stays separate in the entry.
            exit_code = 1 if exit_code in (0, 3) else exit_code
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
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


def run_tick(config, slot, state):
    """Supervise one forecast tick with the shadow tick budget plus grace as its deadline."""
    shadow = config["shadow"]
    tick_config = dict(config, deadline=shadow["tick_budget"] + config["grace"])
    stdout, stderr = state / f"tick-stdout-{slot}.log", state / f"tick-stderr-{slot}.log"
    record = {
        "argv": tick_command(config),
        "stdout": str(stdout),
        "stderr": str(stderr),
        "started_at": utc(),
        "research_config_sha256": digest(shadow["research_config"]),
    }
    mono = time.monotonic()
    try:
        code, reason = supervise(tick_config, record["argv"], stdout, stderr)
        record["exit_code"] = code
        if reason:
            record["status"] = reason
        else:
            try:
                result = json.loads(stdout.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = None
            if not isinstance(result, dict) or result.get("status") not in TICK_STATUSES:
                record["status"] = "invalid_output"
            else:
                record["status"] = result["status"]
                record["run_id"] = result.get("run_id")
                record["decisions_eligible"] = result.get("decisions_eligible")
                record["decisions_abstained"] = result.get("decisions_abstained")
                record["due_opportunities"] = result.get("due_opportunities")
                record["bindings_prepared"] = result.get("bindings_prepared")
                record["publications"] = result.get("publications")
                record["errors"] = str(result.get("errors", []))[:1000]
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        record.update(status="wrapper_failed", error=str(exc)[:1000])
    record["ended_at"] = utc()
    record["runtime_seconds"] = time.monotonic() - mono
    return record


def shadow_health(config, latest):
    """Shadow-specific status: separates 'no eligible markets' from operational failure.

    Uses the latest history entry (tick and collection) only; it does not open the
    archive, so it is available even when the database is locked by a running cycle.
    """
    if latest is None or "tick" not in latest:
        return {"status": "never_invoked", "tick": None, "collection": None}
    tick = latest["tick"]
    collection = latest["status"]
    operational = {
        "failed",
        "deadline_exceeded",
        "interrupted",
        "invalid_output",
        "wrapper_failed",
        "log_write_failed",
        "output_limit_exceeded",
    }
    if tick["status"] in operational or collection in {
        "failed_or_partial",
        "wrapper_failed",
        "deadline_exceeded",
        "interrupted",
        "log_write_failed",
        "output_limit_exceeded",
        "unverified_result",
        "completion_unknown",
    }:
        status = "operational_failure"
    elif (tick.get("decisions_eligible") or 0) + (tick.get("decisions_abstained") or 0) > 0:
        status = "forecasting"
    elif collection == "discovery_no_eligible_games":
        status = "no_eligible_markets"
    elif collection == "collected_eligible_games":
        status = "waiting_for_cutoff"  # eligible games collected; no cutoff due yet
    else:
        status = "inconclusive"
    return {
        "status": status,
        "tick": {
            k: tick.get(k)
            for k in (
                "status",
                "exit_code",
                "run_id",
                "decisions_eligible",
                "decisions_abstained",
                "due_opportunities",
                "bindings_prepared",
                "publications",
                "runtime_seconds",
                "started_at",
                "ended_at",
                "stdout",
                "stderr",
                "errors",
            )
        },
        "collection": collection,
        "meaning": {
            "no_eligible_markets": "discovery completed with no eligible contracts; inconclusive, not a failure",
            "waiting_for_cutoff": "eligible games collected; no decision cutoff reached in the last tick",
            "forecasting": "the last tick recorded at least one decision or explicit abstention",
            "operational_failure": "the tick or the collection failed, timed out or was interrupted",
            "inconclusive": "neither failure nor evidence of eligible markets in the last cycle",
            "never_invoked": "no shadow cycle has run",
        }[status],
    }


def maintenance(config, argv, *, name, deadline):
    """Run one bounded vader command under the operations lock, with its output kept in
    the state directory. A concurrently scheduled cycle skips as an overlap."""
    try:
        with lock(adjacent(config, "run.lockfile")):
            state = Path(config["state"])
            stamp = utc().replace(":", "").replace("+00:00", "Z")
            stdout, stderr = (
                state / f"{name}-{stamp}.stdout.log",
                state / f"{name}-{stamp}.stderr.log",
            )
            started = time.monotonic()
            record = {"name": name, "argv": maintenance_command(config, argv), "started_at": utc()}
            code, reason = supervise(
                dict(config, deadline=deadline), record["argv"], stdout, stderr
            )
            record.update(exit_code=code, stdout=str(stdout), stderr=str(stderr))
            if reason:
                record["status"] = reason
            else:
                try:
                    result = json.loads(stdout.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    result = None
                record["status"] = (
                    result.get("status", "invalid_output")
                    if isinstance(result, dict)
                    else "invalid_output"
                )
                record["result"] = result
            record["ended_at"] = utc()
            record["runtime_seconds"] = time.monotonic() - started
            return record
    except BlockingIOError:
        return {"name": name, "status": "overlap_skipped", "exit_code": 75}


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
    shadow = shadow_health(config, latest) if "shadow" in config else None
    return {
        "status": status,
        "shadow": shadow,
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
