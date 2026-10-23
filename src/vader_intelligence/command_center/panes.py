"""Bounded pane capture for registered workers through the reviewed observer
(`tools.worker_observer`). Only a pane the controller registered for `(run_id,
worker_id)` on its own disposable tmux server is ever read; identity is re-verified by
the observer before and after capture, and a mismatch returns no text."""

import json
import subprocess
import sys
from pathlib import Path

from .config import CenterConfig
from .readers import now_iso
from .store import CenterStore

OBSERVER_CONFIG = "observer.json"
CAPTURE_TIMEOUT = 20


def _unavailable(reason, now=None):
    return {
        "state": "unavailable",
        "reason": reason,
        "generated_at": now_iso(now),
        "source_time": None,
        "identity": "unknown",
    }


def pane_reader(cfg: CenterConfig, run_id, worker_id, lines, now=None, *, runner=subprocess.run):
    """Projection of one registered worker's pane: `identity`, bounded `text`."""
    try:
        with CenterStore(cfg.root, readonly=True) as center:
            worker = center.worker(run_id, worker_id)
    except FileNotFoundError:
        worker = None
    if worker is None:
        return {
            "state": "empty",
            "reason": "no such registered worker",
            "generated_at": now_iso(now),
            "source_time": None,
            "identity": "unknown",
        }
    if not worker.get("pane_id"):
        return _unavailable("worker has no registered pane", now)
    config_path = cfg.root / "tmux" / OBSERVER_CONFIG
    if not config_path.is_file():
        return _unavailable(
            "observer configuration absent (controller not running or no tmux)", now
        )
    try:
        observer_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _unavailable("observer configuration unreadable", now)
    registered = [
        w
        for w in observer_config.get("workers", [])
        if w.get("worker_id") == f"{run_id}:{worker_id}"
    ]
    if not registered:
        return _unavailable("worker not registered with the observer", now)
    python = observer_config.get("upstream_python") or sys.executable
    argv = [
        python,
        "-m",
        "tools.worker_observer",
        "--config",
        str(config_path),
        "--worker",
        f"{run_id}:{worker_id}",
        "inspect",
    ]
    checkout = str(Path(__file__).resolve().parents[3])
    try:
        done = runner(argv, capture_output=True, text=True, timeout=CAPTURE_TIMEOUT, cwd=checkout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _unavailable(f"observer failed: {type(exc).__name__}", now)
    try:
        report = json.loads(done.stdout) if done.stdout.strip() else {}
    except ValueError:
        return _unavailable("observer output unreadable", now)
    identity = report.get("identity", "unknown")
    if done.returncode != 0 or identity != "matched":
        return {
            "state": "unavailable",
            "reason": f"pane identity {identity}: {report.get('reason') or 'observer exit ' + str(done.returncode)}",
            "generated_at": now_iso(now),
            "source_time": None,
            "identity": identity,
            "text": None,
        }
    text = report.get("terminal")
    if not isinstance(text, str):
        text = None
    else:
        text = "\n".join(text.splitlines()[-lines:])
    return {
        "state": "ok",
        "generated_at": now_iso(now),
        "source_time": report.get("observed_at"),
        "observed_at": report.get("observed_at"),
        "identity": identity,
        "pane": report.get("pane"),
        "text": text,
        "completion_verified": False,
    }
