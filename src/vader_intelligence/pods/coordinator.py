"""Coordinator: the only component that spawns, reaps, kills and ingests pod workers.

`step(now)` is finite: (1) reap attempts whose worker exited, whose wall deadline passed
or whose lease expired (killing the worker's process group first), ingesting a result
manifest when one exists; (2) cancel: SIGTERM the group of a cancel-requested attempt,
then SIGKILL it after the grace period and record `canceled`; (3) schedule due tasks by
priority within each pod's concurrency and daily budgets. `run()` repeats `step()` under
`max_steps` and a wall deadline. All durable state lives in the store, so a new
coordinator recovers from the database alone; `shutdown()` ingests finished work, kills
and records every live attempt, and releases the coordinator lock.

Process handling (lessons from tools/worker_observer):

- Each worker leads its own session/process group (`start_new_session=True`). A group is
  signalled only while its leader is known to be unreaped (an owned child we have not
  waited for) or after the leader's command line has been re-verified (`ps`) to be this
  attempt's worker, so a recycled PID is never signalled.
- Group signals on macOS can return EPERM for a zombie-only group; that is recorded, not
  raised. SIGKILL precedes the final wait, so stragglers in the group are included.
- Requires the default SIGCHLD disposition and exclusive ownership of child waits, and
  must be driven from one thread (`preexec_fn` applies resource limits before exec).

Isolation here bounds faults and resources (separate process, scrubbed environment,
private working directory, rlimits, wall deadline). It is not a security sandbox: a
worker runs as the same OS user and could open any file that user can; pods are
restricted by running only declarative operations from the approved registry, and no
credential, risk-limit, evaluator, holdout or archive path is ever passed to them.

Platform limits of `resource.setrlimit`: RLIMIT_CPU, RLIMIT_NOFILE and RLIMIT_FSIZE are
applied everywhere, but each is per process (a worker's children get their own copy,
not a shared allowance) and RLIMIT_FSIZE bounds the size of each written file, not total
disk use. `memory_mb` maps to RLIMIT_AS except on macOS, where RLIMIT_AS is not enforced
(setting it fails); there the leased event records `memory_enforced: false`. Wall time
is enforced only by the coordinator's deadline kill.
"""

import fcntl
import hmac
import math
import os
import resource
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactCorrupted
from .contracts import MAX_RESULT_MANIFEST_BYTES, ContractError, parse_result_manifest, to_plain
from .store import SHUTDOWN_REASON, OrchestrationStore, StaleAttempt
from .worker import EXIT_CLAIM_REFUSED, EXIT_LEASE_LOST, MANIFEST_NAME

WORKER_MODULE = "vader_intelligence.pods.worker"
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "PYTHONHASHSEED", "TZ")
ENV_PREFIX = "VI_TASK_"
PS_PATH = "/bin:/usr/bin"


class CoordinatorLocked(RuntimeError):
    """Another coordinator holds this orchestration root."""


class UnsafeFile(ValueError):
    """A worker-written path is missing, a link, not a regular file, or oversized."""


@dataclass(frozen=True)
class CoordinatorConfig:
    heartbeat_seconds: float = 5.0
    lease_seconds: float = 30.0
    cancel_grace_seconds: float = 5.0
    backoff_base_seconds: float = 10.0
    backoff_max_seconds: float = 900.0
    poll_seconds: float = 0.25
    kill_wait_seconds: float = 5.0
    max_open_files: int = 256
    max_file_bytes: int = 1 << 30
    python: str = sys.executable

    def __post_init__(self):
        if not 0 < self.heartbeat_seconds <= self.lease_seconds / 2:
            raise ValueError("heartbeat_seconds must be positive and at most lease_seconds / 2")
        if self.cancel_grace_seconds < 0 or self.kill_wait_seconds <= 0:
            raise ValueError("grace and kill wait must be non-negative / positive")
        if not 0 < self.backoff_base_seconds <= self.backoff_max_seconds:
            raise ValueError("backoff must satisfy 0 < base <= max")
        if self.poll_seconds <= 0 or self.max_open_files < 32 or self.max_file_bytes < 1 << 20:
            raise ValueError("poll_seconds, max_open_files or max_file_bytes too small")


def worker_environment(attempt: dict, db_path, config: CoordinatorConfig) -> dict:
    """The complete worker environment: the allowlist plus VI_TASK_* values, nothing else."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": attempt["workdir"],
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
        "VI_TASK_ID": attempt["task_id"],
        "VI_TASK_ATTEMPT_ID": attempt["attempt_id"],
        "VI_TASK_LEASE_TOKEN": attempt["lease_token"],
        "VI_TASK_DB": str(db_path),
        "VI_TASK_HEARTBEAT_SECONDS": repr(config.heartbeat_seconds),
        "VI_TASK_LEASE_SECONDS": repr(config.lease_seconds),
    }
    if "LANG" in os.environ:
        env["LANG"] = os.environ["LANG"]
    if not all(key in ENV_ALLOWLIST or key.startswith(ENV_PREFIX) for key in env):
        raise RuntimeError("worker environment outside the allowlist")
    return env


def _bounded(kind, value):
    _, hard = resource.getrlimit(kind)
    return value if hard == resource.RLIM_INFINITY else min(value, hard)


def _preexec(limits: dict):
    """Runs in the child between fork and exec: only setrlimit calls."""
    soft = _bounded(resource.RLIMIT_CPU, limits["cpu_seconds"])
    cpu = (soft, _bounded(resource.RLIMIT_CPU, limits["cpu_seconds"] + 1))
    nofile = (limits["nofile"], limits["nofile"])
    fsize = (limits["fsize_bytes"], limits["fsize_bytes"])
    memory = limits["memory_bytes"]

    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, cpu)
        resource.setrlimit(resource.RLIMIT_NOFILE, nofile)
        resource.setrlimit(resource.RLIMIT_FSIZE, fsize)
        if memory is not None:
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

    return apply


def _signal_group(pgid: int, signum: int) -> str:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return "no_such_group"
    except PermissionError:
        return "eperm"  # e.g. a zombie-only group on macOS
    return signal.Signals(signum).name


def _identity(pid: int, attempt_id: str) -> bool | None:
    """True if `pid` runs this attempt's worker, False if not, None if unverifiable."""
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": PS_PATH, "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return False
    argv = out.stdout.split()
    return WORKER_MODULE in argv and attempt_id in argv


def _gone(pid: int, owned: set[int]) -> bool:
    """Whether a worker this instance did not spawn has exited.

    A PID now held by one of our own children was recycled, so the worker is gone; it
    is never waited for here (that would reap our child behind its handle). Otherwise a
    zombie left by an earlier coordinator object in this process is reaped.
    """
    if pid in owned:
        return True
    try:
        if os.waitpid(pid, os.WNOHANG)[0]:
            return True
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def _read_regular(directory: Path, name: str, limit: int) -> bytes:
    """Read a regular, unlinked-to file directly inside `directory` (no symlinks)."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise UnsafeFile(f"{directory.name}: {type(exc).__name__}") from None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise UnsafeFile(f"{name}: {type(exc).__name__}") from None
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise UnsafeFile(f"{name}: not a bounded regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise UnsafeFile(f"{name}: too large")
    return data


def classify_exit(code: int | None) -> tuple[str, str, bool]:
    """(outcome, reason, retryable) for a worker that exited without a result manifest."""
    if code == -signal.SIGXCPU:
        return "failed", "cpu_limit", False
    if code == EXIT_LEASE_LOST:
        return "lost", "lease_lost", True
    if code == EXIT_CLAIM_REFUSED:
        return "lost", "claim_refused", True
    if code is None:
        return "lost", "process_gone", True
    if code < 0:
        return "lost", f"signal_{-code}", True
    return "lost", f"exit_{code}", True


class Coordinator:
    """Single coordinator per orchestration root (held by an exclusive flock)."""

    def __init__(self, store: OrchestrationStore, config: CoordinatorConfig | None = None):
        if store.readonly:
            raise ValueError("the coordinator needs a writable store")
        if signal.getsignal(signal.SIGCHLD) not in (signal.SIG_DFL, None):
            raise RuntimeError("default SIGCHLD disposition required (exclusive child waits)")
        self.store = store
        self.config = config or CoordinatorConfig()
        self._lock = open(store.root / "coordinator.lock", "a", encoding="utf-8")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise CoordinatorLocked("another coordinator holds this orchestration root") from None
        self._children: dict[str, subprocess.Popen] = {}
        self._verified: set[tuple[int, str]] = set()
        self._terminated: set[str] = set()
        self._strays: list[subprocess.Popen] = []
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()

    def _check_open(self):
        if self._closed:
            raise RuntimeError("coordinator is shut down")

    def submit(self, task, now=None) -> str:
        return self.store.submit(task, now)

    def cancel(self, task_id, now=None) -> str:
        return self.store.request_cancel(task_id, now)

    # Loop ------------------------------------------------------------------------------

    def step(self, now=None) -> dict:
        """One finite pass: reap/ingest, cancel, schedule. Returns what happened."""
        self._check_open()
        at = time.time() if now is None else float(now)
        report = {"finalized": [], "spawned": [], "signaled": []}
        self._reap(at, report)
        self._cancel(at, report)
        self._schedule(at, report)
        self._reap_strays()
        report["active"] = len(self.store.open_attempts())
        return report

    def run(self, max_steps: int, deadline_seconds: float, *, stop_when_idle=True) -> dict:
        """Repeat `step()` at most `max_steps` times and for at most `deadline_seconds`.

        Stops early when idle: nothing running, nothing spawned and no retry pending
        (tasks blocked only by a budget stay queued). Running workers are left running
        for the next run; use `shutdown()` (or the context manager) to stop them.
        """
        self._check_open()
        if max_steps < 1 or deadline_seconds <= 0:
            raise ValueError("run() needs max_steps >= 1 and a positive deadline")
        end = time.monotonic() + deadline_seconds
        steps, stopped = 0, "max_steps"
        while steps < max_steps:
            if time.monotonic() >= end:
                stopped = "deadline"
                break
            report = self.step()
            steps += 1
            if stop_when_idle and not report["active"] and not report["spawned"]:
                if self.store.waiting_retries() == 0:
                    stopped = "idle"
                    break
            time.sleep(max(0.0, min(self.config.poll_seconds, end - time.monotonic())))
        return {"steps": steps, "stopped": stopped}

    def shutdown(self, now=None) -> dict:
        """Ingest finished work, kill and record every live attempt, release the lock."""
        if self._closed:
            return {"killed": [], "unreaped": []}
        at = time.time() if now is None else float(now)
        killed = []
        try:
            self._reap(at, {"finalized": [], "spawned": [], "signaled": []})
            for row in self.store.open_attempts():
                detail = self._kill(row)
                if row["cancel_requested_at"] is not None:
                    self._finish(row, at, "canceled", "cancel_requested", False, detail)
                else:
                    self._conclude(row, at, None, ("lost", SHUTDOWN_REASON, True), detail)
                killed.append(row["attempt_id"])
        finally:
            # Even if recording failed, no owned process outlives the coordinator.
            leftovers = [*self._children.values(), *self._strays]
            for child in leftovers:
                if child.returncode is None:
                    _signal_group(child.pid, signal.SIGKILL)
                    self._wait(child)
            self._closed = True
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
        unreaped = [child.pid for child in leftovers if child.returncode is None]
        return {"killed": killed, "unreaped": unreaped}

    # Phases ----------------------------------------------------------------------------

    def _reap(self, at, report):
        for row in self.store.open_attempts():
            if row["cancel_requested_at"] is not None:
                continue
            exited, code = self._exited(row)
            if exited:
                report["finalized"].append(self._conclude(row, at, code, None, {}))
            elif at >= row["deadline_at"]:
                detail = self._kill(row)
                fallback = ("failed", "wall_deadline", True)
                report["finalized"].append(self._conclude(row, at, None, fallback, detail))
            elif at >= row["lease_expires_at"]:
                detail = self._kill(row)
                fallback = ("lost", "lease_expired", True)
                report["finalized"].append(self._conclude(row, at, None, fallback, detail))

    def _cancel(self, at, report):
        for row in self.store.open_attempts():
            if row["cancel_requested_at"] is None:
                continue
            attempt_id = row["attempt_id"]
            if at < row["cancel_requested_at"] + self.config.cancel_grace_seconds:
                if attempt_id not in self._terminated:
                    self._terminated.add(attempt_id)
                    self._signal(row, signal.SIGTERM)
                    report["signaled"].append(attempt_id)
                continue
            detail = self._kill(row)
            self._terminated.discard(attempt_id)
            report["finalized"].append(
                self._finish(row, at, "canceled", "cancel_requested", False, detail)
            )

    def _schedule(self, at, report):
        budgets, usage = {}, {}
        for task in self.store.schedulable_tasks(at):
            pod = task["pod_id"]
            limit = budgets.setdefault(pod, self.store.budgets(pod))
            used = usage.setdefault(pod, self.store.pod_usage(pod, at))
            if (
                used["active"] >= limit["max_concurrent"]
                or used["attempts_today"] >= limit["tasks_per_day"]
                or used["cpu_seconds_today"] >= limit["cpu_seconds_per_day"]
            ):
                continue
            remaining_cpu = limit["cpu_seconds_per_day"] - used["cpu_seconds_today"]
            self._spawn(task["task_id"], limit, remaining_cpu, at, report)
            used["active"] += 1
            used["attempts_today"] += 1

    # Spawning --------------------------------------------------------------------------

    def _limits(self, budgets, remaining_cpu) -> dict:
        wall = budgets["wall_seconds_per_task"]
        memory = int(budgets["memory_mb"]) << 20
        enforced = sys.platform != "darwin"  # RLIMIT_AS is not enforced on macOS
        return {
            "cpu_seconds": max(1, math.ceil(min(wall, remaining_cpu))),
            "nofile": _bounded(resource.RLIMIT_NOFILE, self.config.max_open_files),
            "fsize_bytes": _bounded(resource.RLIMIT_FSIZE, self.config.max_file_bytes),
            "memory_bytes": memory if enforced else None,
            "memory_enforced": enforced,
        }

    def _spawn(self, task_id, budgets, remaining_cpu, at, report):
        limits = self._limits(budgets, remaining_cpu)
        try:
            attempt = self.store.create_attempt(
                task_id,
                at,
                lease_seconds=self.config.lease_seconds,
                deadline_at=at + budgets["wall_seconds_per_task"],
                limits=limits,
            )
        except StaleAttempt:
            return  # canceled or leased by another writer since the query; skip it
        attempt_id = attempt["attempt_id"]
        workdir = Path(attempt["workdir"])
        try:
            workdir.mkdir(mode=0o700)
            (workdir / "out").mkdir(mode=0o700)
            with open(attempt["log_path"], "ab") as log:
                child = subprocess.Popen(
                    [self.config.python, "-s", "-P", "-m", WORKER_MODULE, "--attempt", attempt_id],
                    cwd=workdir,
                    env=worker_environment(attempt, self.store.path, self.config),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    preexec_fn=_preexec(limits),
                )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            report["finalized"].append(
                self._finish(attempt, at, "lost", "spawn_failed", True, detail)
            )
            return
        self._children[attempt_id] = child
        report["spawned"].append(attempt_id)

    # Process state ---------------------------------------------------------------------

    def _exited(self, row) -> tuple[bool, int | None]:
        child = self._children.get(row["attempt_id"])
        if child is not None:
            return self._poll(child)
        pid, attempt_id = row["worker_pid"], row["attempt_id"]
        if pid is None:
            return False, None  # not yet claimed; the lease bounds it
        if _gone(pid, self._owned_pids()):
            return True, None
        if (pid, attempt_id) in self._verified:
            return False, None  # a recycled PID only delays detection until the lease
        verified = _identity(pid, attempt_id)
        if verified:
            self._verified.add((pid, attempt_id))
        return verified is False, None

    def _owned_pids(self) -> set[int]:
        return {child.pid for child in [*self._children.values(), *self._strays]}

    @staticmethod
    def _poll(child) -> tuple[bool, int | None]:
        """Non-blocking reap of an owned child; records its rusage CPU on the handle."""
        if child.returncode is not None:
            return True, child.returncode
        try:
            pid, status, usage = os.wait4(child.pid, os.WNOHANG)
        except ChildProcessError:
            child.returncode = -1  # reaped elsewhere: exclusive waits were violated
            return True, None
        if pid == 0:
            return False, None
        child.returncode = os.waitstatus_to_exitcode(status)
        child.vi_cpu_seconds = usage.ru_utime + usage.ru_stime
        return True, child.returncode

    def _wait(self, child) -> None:
        deadline = time.monotonic() + self.config.kill_wait_seconds
        while not self._poll(child)[0] and time.monotonic() < deadline:
            time.sleep(0.01)

    def _signal(self, row, signum) -> str:
        child = self._children.get(row["attempt_id"])
        if child is not None:
            return "reaped" if child.returncode is not None else _signal_group(child.pid, signum)
        pid = row["worker_pid"]
        if pid is None:
            return "no_pid"
        verified = _identity(pid, row["attempt_id"])
        if verified is not True:
            return "identity_unknown" if verified is None else "gone"
        return _signal_group(pid, signum)

    def _kill(self, row) -> dict:
        """SIGKILL the attempt's process group (never a recycled PID) and reap if owned."""
        attempt_id = row["attempt_id"]
        child = self._children.get(attempt_id)
        if child is None:
            outcome = self._signal(row, signal.SIGKILL)
            pid = row["worker_pid"]
            if outcome == "SIGKILL":
                deadline = time.monotonic() + self.config.kill_wait_seconds
                while not _gone(pid, self._owned_pids()) and time.monotonic() < deadline:
                    time.sleep(0.01)
            return {"kill": outcome, "pid": pid}
        outcome = "reaped"
        if child.returncode is None:
            outcome = _signal_group(child.pid, signal.SIGKILL)
            self._wait(child)
        return {"kill": outcome, "pid": child.pid, "exit_code": child.returncode}

    def _reap_strays(self):
        self._strays = [child for child in self._strays if not self._poll(child)[0]]

    def _release(self, attempt_id) -> float | None:
        """Forget an attempt's child handle (unreaped ones stay tracked); return its CPU."""
        child = self._children.pop(attempt_id, None)
        if child is None:
            return None
        if child.returncode is None:
            self._strays.append(child)
        return getattr(child, "vi_cpu_seconds", None)

    # Finalization ----------------------------------------------------------------------

    def _backoff(self, n) -> float:
        cfg = self.config
        return min(cfg.backoff_max_seconds, cfg.backoff_base_seconds * 2 ** (n - 1))

    def _finish(self, row, at, outcome, reason, retryable, detail, cpu=None) -> dict:
        attempt_id = row["attempt_id"]
        owned_cpu = self._release(attempt_id)
        try:
            self.store.finish_attempt(
                attempt_id,
                outcome,
                reason,
                at,
                retryable=retryable,
                retry_delay=self._backoff(row["n"]),
                cpu_seconds=cpu if owned_cpu is None else owned_cpu,
                detail=detail,
            )
        except StaleAttempt:
            return {"attempt_id": attempt_id, "outcome": None, "reason": "already_closed"}
        return {"attempt_id": attempt_id, "outcome": outcome, "reason": reason}

    def _conclude(self, row, at, code, fallback, detail) -> dict:
        """Finalize a worker that is no longer running: ingest its manifest if present."""
        workdir = Path(row["workdir"])
        if os.path.lexists(workdir / MANIFEST_NAME):
            return self._ingest(row, at, detail)
        outcome, reason, retryable = fallback or classify_exit(code)
        return self._finish(row, at, outcome, reason, retryable, dict(detail, exit_code=code))

    def _ingest(self, row, at, detail) -> dict:
        workdir = Path(row["workdir"])
        try:
            manifest = parse_result_manifest(
                _read_regular(workdir, MANIFEST_NAME, MAX_RESULT_MANIFEST_BYTES)
            )
        except (ContractError, UnsafeFile) as exc:
            return self._quarantine(row, at, "malformed_manifest", exc, detail)
        if manifest.task_id != row["task_id"] or manifest.attempt_id != row["attempt_id"]:
            return self._quarantine(row, at, "identity_mismatch", None, detail)
        if not hmac.compare_digest(manifest.lease_token, row["lease_token"]):
            return self._quarantine(row, at, "lease_token_mismatch", None, detail)
        attempt_id = row["attempt_id"]
        reported_cpu = manifest.worker.cpu_seconds
        if manifest.status == "failed":
            error = manifest.error
            info = dict(detail, error=to_plain(error), worker_pid=manifest.worker.pid)
            return self._finish(row, at, "failed", error.type, error.retryable, info, reported_cpu)
        for ref in manifest.artifacts:
            try:
                self._copy_artifact(workdir / "out", ref)
            except ArtifactCorrupted as exc:
                return self._quarantine(row, at, "artifact_hash_mismatch", exc, detail)
            except UnsafeFile as exc:
                return self._quarantine(row, at, "artifact_missing_or_unsafe", exc, detail)
        owned_cpu = self._release(attempt_id)
        verdict = self.store.ingest_result(
            attempt_id,
            row["lease_token"],
            manifest,
            at,
            cpu_seconds=reported_cpu if owned_cpu is None else owned_cpu,
        )
        return {"attempt_id": attempt_id, "outcome": manifest.status, "reason": verdict}

    def _copy_artifact(self, out_dir: Path, ref) -> None:
        try:
            dir_fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise UnsafeFile(f"out: {type(exc).__name__}") from None
        try:
            fd = os.open(ref.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError as exc:
            raise UnsafeFile(f"{ref.name}: {type(exc).__name__}") from None
        finally:
            os.close(dir_fd)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise UnsafeFile(f"{ref.name}: not a regular file")
            if info.st_size != ref.bytes:
                raise ArtifactCorrupted(f"{ref.name}: size differs from the manifest")
            self.store.artifacts.put_stream(handle, expected_sha256=ref.sha256, max_bytes=ref.bytes)

    def _quarantine(self, row, at, reason, exc, detail) -> dict:
        info = dict(detail)
        if exc is not None:
            info["error"] = str(exc)[:500]
        return self._finish(row, at, "quarantined", reason, False, info)
