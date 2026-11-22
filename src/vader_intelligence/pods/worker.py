"""Pod worker: `python -m vader_intelligence.pods.worker --attempt ATTEMPT_ID`.

Spawned only by the coordinator, in a new session, with a scrubbed environment, resource
limits and its attempt directory as working directory and HOME. It:

1. claims the attempt with its lease token (refused → exit 78, nothing written);
2. renews the lease from a heartbeat thread through short transactions and stops itself
   (exit 75) as soon as a renewal is refused or the lease lapses;
3. runs exactly one approved operation from the registry with typed parameters;
4. writes `result.json` atomically into the attempt directory and exits 0.

The worker never writes task state, results or artifacts in the store: the coordinator
validates the manifest and records everything. This process boundary bounds faults and
resources; it is not a security sandbox.
"""

import argparse
import json
import os
import resource
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

from .contracts import RESULT_SCHEMA, ContractError, ErrorInfo, ResultManifest, WorkerInfo
from .operations import NOT_RETRYABLE, OperationContext, Partial, get_operation
from .store import claim_attempt, iso, renew_lease, worker_connection

EXIT_INTERNAL = 70
EXIT_LEASE_LOST = 75
EXIT_CLAIM_REFUSED = 78
MANIFEST_NAME = "result.json"


def _log(message: str) -> None:
    print(f"{iso(time.time())} worker[{os.getpid()}] {message}", file=sys.stderr, flush=True)


def cpu_seconds() -> float:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def observed_rlimits() -> dict:
    names = {"cpu": "RLIMIT_CPU", "nofile": "RLIMIT_NOFILE", "fsize": "RLIMIT_FSIZE"}
    names["as"] = "RLIMIT_AS"
    return {key: list(resource.getrlimit(getattr(resource, name))) for key, name in names.items()}


class Heartbeat(threading.Thread):
    """Renews the lease every `interval`; exits the whole process when it cannot."""

    def __init__(self, db_path, attempt_id, token, interval, lease_seconds, expires_at):
        super().__init__(name="lease-heartbeat", daemon=True)
        self.db_path = db_path
        self.attempt_id = attempt_id
        self.token = token
        self.interval = interval
        self.lease_seconds = lease_seconds
        self.expires_at = expires_at
        self._halt = threading.Event()

    def run(self):
        db = None
        try:
            db = worker_connection(self.db_path)
            while not self._halt.wait(self.interval):
                now = time.time()
                try:
                    renewed = renew_lease(
                        db, self.attempt_id, self.token, now, self.lease_seconds, cpu_seconds()
                    )
                except sqlite3.Error as exc:
                    _log(f"lease renewal error: {type(exc).__name__}")
                    renewed = None
                if renewed:
                    self.expires_at = now + self.lease_seconds
                elif renewed is False or time.time() >= self.expires_at:
                    _log("lease lost or revoked; stopping")
                    os._exit(EXIT_LEASE_LOST)
        except BaseException:
            traceback.print_exc()
            os._exit(EXIT_LEASE_LOST)
        finally:
            if db is not None:
                db.close()

    def stop(self):
        self._halt.set()
        self.join(timeout=self.interval + 10)


def execute(claim, attempt_id, token, workdir, env_names) -> ResultManifest:
    """Run the claimed operation; every exception becomes a failed manifest."""
    ctx = OperationContext(claim["task_id"], attempt_id, claim["n"], workdir, claim["deadline_at"])

    def manifest(status, output=None, error=None):
        return ResultManifest(
            schema=RESULT_SCHEMA,
            task_id=claim["task_id"],
            attempt_id=attempt_id,
            lease_token=token,
            status=status,
            output=output,
            error=error,
            artifacts=tuple(ctx.artifacts) if status != "failed" else (),
            worker=WorkerInfo(os.getpid(), tuple(env_names), observed_rlimits(), cpu_seconds()),
        )

    try:
        spec = get_operation(claim["type"])
        params = spec.parse(json.loads(claim["params_json"]))
        value = spec.run(params, ctx)
        status = "succeeded"
        if isinstance(value, Partial):
            status, value = "partial", value.output
        if not isinstance(value, dict):
            raise ContractError("operation output must be a JSON object")
        return manifest(status, output=value)
    except Exception as exc:
        traceback.print_exc()
        retryable = not isinstance(exc, NOT_RETRYABLE)
        error = ErrorInfo(type(exc).__name__, str(exc)[:2000], retryable)
        return manifest("failed", error=error)


def write_manifest(workdir: Path, manifest: ResultManifest) -> None:
    """Atomic publish: write + fsync a temp file, rename, fsync the directory."""
    data = manifest.to_bytes()
    temp = workdir / (MANIFEST_NAME + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, workdir / MANIFEST_NAME)
    directory = os.open(workdir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vader_intelligence.pods.worker")
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args(argv)
    env_names = sorted(os.environ)
    try:
        attempt_id = os.environ["VI_TASK_ATTEMPT_ID"]
        token = os.environ.pop("VI_TASK_LEASE_TOKEN")
        db_path = os.environ["VI_TASK_DB"]
        interval = float(os.environ["VI_TASK_HEARTBEAT_SECONDS"])
        lease_seconds = float(os.environ["VI_TASK_LEASE_SECONDS"])
    except (KeyError, ValueError):
        _log("missing or invalid VI_TASK_* environment; refusing")
        return EXIT_CLAIM_REFUSED
    if attempt_id != args.attempt or os.getpgid(0) != os.getpid():
        _log("attempt mismatch or not a process-group leader; refusing")
        return EXIT_CLAIM_REFUSED
    workdir = Path.cwd()
    db = worker_connection(db_path)
    try:
        claim = claim_attempt(db, attempt_id, token, os.getpid(), time.time(), lease_seconds)
    finally:
        db.close()
    if claim is None:
        _log("lease token refused; exiting without running")
        return EXIT_CLAIM_REFUSED
    _log(f"claimed {attempt_id} type={claim['type']}")
    heartbeat = Heartbeat(
        db_path, attempt_id, token, interval, lease_seconds, claim["lease_expires_at"]
    )
    heartbeat.start()
    try:
        manifest = execute(claim, attempt_id, token, workdir, env_names)
        write_manifest(workdir, manifest)
        _log(f"wrote {MANIFEST_NAME} status={manifest.status}")
    finally:
        heartbeat.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        sys.exit(EXIT_INTERNAL)
