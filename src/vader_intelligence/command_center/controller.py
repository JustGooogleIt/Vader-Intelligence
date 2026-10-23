"""The Command Center controller: the single process that holds the pods coordinator
for a root, applies durable commands, dispatches farm stages, registers panes on its
own disposable tmux server and reconciles existing attempts on start before any new
dispatch. Browser handlers never spawn anything; this loop does."""

import json
import logging
import socket
import sys
import time
from pathlib import Path

from ..pods.coordinator import Coordinator, CoordinatorConfig
from ..pods.store import open_store
from .coding import CodingService
from .config import CenterConfig
from .farms import FarmService
from .store import CenterStore
from .tmux import TmuxServer, TmuxUnavailable

log = logging.getLogger(__name__)
OBSERVER_CONFIG = "observer.json"


class Controller:
    def __init__(
        self,
        cfg: CenterConfig,
        *,
        coordinator_config=None,
        tmux=True,
        observer_python=None,
        now=None,
    ):
        self.cfg = cfg
        self.root = cfg.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.pods = open_store(self.root)
        self.center = CenterStore(self.root)
        self.coordinator = Coordinator(self.pods, coordinator_config or CoordinatorConfig())
        self.coding = CodingService(cfg, self.center)
        self.farms = FarmService(
            cfg,
            self.center,
            self.pods,
            self.coordinator,
            agent_processes=self.coding.live_processes,
        )
        self.now = now
        self.tmux = None
        self.tmux_error = None
        self.observer_python = observer_python or sys.executable
        self.registered = {}  # attempt_id -> worker registration
        if tmux:
            server = TmuxServer(self.root)
            try:
                server.start()
                self.tmux = server
            except TmuxUnavailable as exc:
                self.tmux_error = str(exc)
                log.warning("no tmux observation surface: %s", exc)
        self.started_at = time.time()
        self.steps = 0

    def _now(self):
        return self.now() if callable(self.now) else None

    # One pass ------------------------------------------------------------------------

    def step(self):
        """Reap/ingest/cancel/schedule through the coordinator first (existing attempts
        are reconciled before any new dispatch), then commands, run states, stages, panes."""
        now = self._now()
        report = self.coordinator.step(now)
        finalized = self.farms.reconcile(now)
        commands = self.apply_commands(now)
        submitted = self.farms.dispatch(now)
        maintenance = self.coding.step(now)
        panes = self._register_panes(now)
        self.steps += 1
        return {
            "coordinator": {k: report[k] for k in ("finalized", "spawned", "signaled", "active")},
            "runs": finalized + maintenance,
            "commands": commands,
            "submitted": submitted,
            "panes": panes,
        }

    def apply_commands(self, now=None):
        """Durable commands → the owning service (game farms or maintenance), with receipts."""
        applied = []
        for command in self.center.pending_commands():
            payload = json.loads(command["payload_json"])
            try:
                if command["kind"] == "start":
                    service = self.coding if payload.get("kind") == "maintenance" else self.farms
                    run_id = service.start(payload, now)
                    self.center.settle_command(
                        command["command_id"], "applied", None, now, {"run_id": run_id}
                    )
                    applied.append((command["command_id"], "applied", run_id))
                    continue
                run = self.center.run(command["run_id"] or "")
                if run is None:
                    raise ValueError("unknown run")
                if run["state"] in ("canceled", "completed", "failed"):
                    raise ValueError(f"run is {run['state']}")
                service = self.coding if run["kind"] == "maintenance" else self.farms
                getattr(service, command["kind"])(run, now)
                self.center.settle_command(
                    command["command_id"], "applied", None, now, {"run_id": run["run_id"]}
                )
                applied.append((command["command_id"], "applied", run["run_id"]))
            except ValueError as exc:
                self.center.settle_command(command["command_id"], "rejected", str(exc)[:300], now)
                applied.append((command["command_id"], "rejected", str(exc)[:80]))
        return applied

    def run(self, *, max_steps, deadline_seconds, stop_when_idle=True, poll_seconds=0.25):
        end = time.monotonic() + deadline_seconds
        stopped = "max_steps"
        while self.steps < max_steps:
            if time.monotonic() >= end:
                stopped = "deadline"
                break
            report = self.step()
            if (
                stop_when_idle
                and not report["coordinator"]["active"]
                and not report["submitted"]
                and self.farms.idle()
                and self.coding.idle()
                and self.pods.waiting_retries() == 0
            ):
                stopped = "idle"
                break
            time.sleep(poll_seconds)
        return {"steps": self.steps, "stopped": stopped}

    def shutdown(self):
        """Stop owned workers (recorded `lost`, retried by the next controller), drop
        the observation server and release the locks. Evidence directories stay."""
        if getattr(self, "_closed", False):
            return {"killed": [], "unreaped": []}
        self._closed = True
        for run_id in list(self.coding.processes):
            self.coding._kill(run_id)
        result = self.coordinator.shutdown(self._now())
        if self.tmux is not None:
            self.tmux.kill()
        self.center.close()
        self.pods.close()
        return result

    # Panes -----------------------------------------------------------------------------

    def _register_panes(self, now=None):
        """One pane per live attempt of a Command Center run, tailing its log, with the
        observer's identity options set; the observer config names only these panes."""
        if self.tmux is None:
            return []
        live = {row["attempt_id"]: row for row in self.pods.open_attempts()}
        for attempt_id in list(self.registered):
            if attempt_id not in live:
                self.registered.pop(attempt_id)
        opened = []
        for attempt_id, row in live.items():
            if attempt_id in self.registered or len(self.registered) >= 16:
                continue
            run_id = self.center.task_run(row["task_id"])
            if run_id is None:
                continue  # not a Command Center task: never observed by this controller
            run = self.center.run(run_id)
            role = next(
                (t["role"] for t in run["tasks"] if t["task_id"] == row["task_id"]), "worker"
            )
            worker_id = f"{role}-{row['n']}"
            checkout = self.root / "tmux" / "work" / run_id / worker_id
            checkout.mkdir(parents=True, exist_ok=True)
            log_path = row["log_path"]
            try:
                pane = self.tmux.open_pane(
                    f"{role}-{attempt_id[:6]}", checkout, f"tail -n 50 -F {json.dumps(log_path)}"
                )
                token = self.tmux.set_identity(
                    pane["pane_id"], f"{run_id}:{worker_id}", row["task_id"], run_id
                )
            except TmuxUnavailable as exc:
                log.warning("pane registration failed for %s: %s", attempt_id, exc)
                continue
            registration = {
                "worker_id": f"{run_id}:{worker_id}",
                "role": role,
                "pane_id": pane["pane_id"],
                "pane_pid": pane["pane_pid"],
                "pane_token": token,
                "checkout": str(checkout),
                "task_id": row["task_id"],
                "run_id": run_id,
            }
            self.registered[attempt_id] = registration
            self.center.register_worker(
                run_id,
                worker_id,
                role,
                attempt_id,
                {"pane_id": pane["pane_id"], "pane_pid": pane["pane_pid"], "pane_token": token},
                now,
            )
            opened.append(pane["pane_id"])
        self._write_observer_config()
        return opened

    def _write_observer_config(self):
        path = self.root / "tmux" / OBSERVER_CONFIG
        if not self.registered:
            try:
                path.unlink()
            except OSError:
                pass
            return
        config = {
            "version": 1,
            "upstream_python": str(self.observer_python),
            "tmux": str(self.tmux.tmux),
            "workspace": str(self.root),
            "host": socket.gethostname(),
            "socket_path": str(self.tmux.socket),
            "server_pid": self.tmux.server_pid,
            "server_started": self.tmux.server_started,
            "allow_external": False,
            "workers": list(self.registered.values()),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, indent=1), encoding="utf-8")
        tmp.replace(path)


def run_controller(
    cfg: CenterConfig,
    *,
    max_steps,
    deadline_seconds,
    stop_when_idle=False,
    coordinator_config=None,
    tmux=True,
    observer_python=None,
):
    controller = Controller(
        cfg, coordinator_config=coordinator_config, tmux=tmux, observer_python=observer_python
    )
    try:
        report = controller.run(
            max_steps=max_steps, deadline_seconds=deadline_seconds, stop_when_idle=stop_when_idle
        )
    finally:
        shutdown = controller.shutdown()
    report["shutdown"] = shutdown
    report["tmux"] = None if controller.tmux is None else str(controller.tmux.socket)
    report["tmux_error"] = controller.tmux_error
    report["root"] = str(cfg.root)
    return report


def observer_config_path(root):
    return Path(root) / "tmux" / OBSERVER_CONFIG
