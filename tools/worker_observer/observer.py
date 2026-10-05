"""Explicit local registration, bounded observations, non-authoritative reports."""

import hashlib
import json
import math
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path

from .boundary import Unavailable, decode, run, workspace_bytes

IDENTITY = (
    "pid",
    "start_time",
    "socket_path",
    "pane_id",
    "pane_pid",
    "@vader_worker",
    "@vader_task",
    "@vader_run",
    "@vader_pane_token",
)
META = (
    "pane_id",
    "session_name",
    "window_name",
    "pane_current_command",
    "pane_current_path",
    "pane_title",
)
STATES = {"done", "running", "awaiting_input", "failed", "unclear"}


def stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise Unavailable("invalid_timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.utcoffset() is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except ValueError:
        raise Unavailable("invalid_timestamp") from None


def text(value, maximum=256):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise Unavailable("invalid_text")
    return value


def configuration(data):
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise Unavailable("config_version")
    for key in ("upstream_python", "tmux", "workspace", "socket_path"):
        if not Path(text(data.get(key), 1024)).is_absolute():
            raise Unavailable("absolute_paths_required")
    if Path(data["tmux"]).name not in ("tmux", "tmux.exe") or "," in data["socket_path"]:
        raise Unavailable("invalid_tmux_configuration")
    for key in ("host", "server_pid", "server_started"):
        text(data.get(key))
    if (
        data.get("allow_external", False) not in (True, False)
        or type(data.get("allow_external", False)) is not bool
    ):
        raise Unavailable("invalid_external_setting")
    workers = data.get("workers")
    if not isinstance(workers, list) or not 1 <= len(workers) <= 16:
        raise Unavailable("worker_limit")
    ids, panes = set(), set()
    for w in workers:
        if not isinstance(w, dict):
            raise Unavailable("invalid_worker")
        for key in ("worker_id", "role", "task_id", "run_id", "pane_pid", "pane_token"):
            text(w.get(key))
        if not re.fullmatch(r"%[0-9]+", text(w.get("pane_id"))):
            raise Unavailable("invalid_pane")
        if not Path(text(w.get("checkout"), 1024)).is_absolute():
            raise Unavailable("absolute_checkout_required")
        Path(w["checkout"]).resolve(strict=True).relative_to(
            Path(data["workspace"]).resolve(strict=True)
        )
        if w["worker_id"] in ids or w["pane_id"] in panes:
            raise Unavailable("duplicate_worker_or_pane")
        ids.add(w["worker_id"])
        panes.add(w["pane_id"])
    return data


class Observer:
    def __init__(self, config, runner=run):
        self.config = configuration(config)
        self.run = runner
        self.env = dict(os.environ)
        # Never inherit another tmux server or local provider override.
        self.env["TMUX"] = f"{config['socket_path']},{config['server_pid']},0"
        self.env["PATH"] = (
            str(Path(config["tmux"]).parent) + os.pathsep + os.environ.get("PATH", "")
        )
        self.env.pop("TMUX_PANE", None)
        self.env.pop("PYTHONPATH", None)
        self.env.pop("JEV_BASE_URL", None)
        self.env.pop("JEV_API_KEY", None)

    def upstream(self, *args, external=False, payload=None):
        env = self.env.copy()
        if external and "JEV_API_KEY" in os.environ:
            env["JEV_API_KEY"] = os.environ["JEV_API_KEY"]
        return self.run(
            [
                self.config["upstream_python"],
                "-I",
                str(Path(__file__).with_name("upstream.py")),
                *args,
            ],
            env=env,
            timeout=20 if external else 5,
            limit=16384 if args[0] == "capture" else 131072,
            payload=payload,
        )

    def identity(self, worker):
        raw = (
            self.run(
                [
                    self.config["tmux"],
                    "-S",
                    self.config["socket_path"],
                    "display-message",
                    "-p",
                    "-t",
                    worker["pane_id"],
                    "\x1f".join("#{" + k + "}" for k in IDENTITY),
                ],
                env=self.env,
                timeout=5,
                limit=4096,
            )
            .rstrip("\n")
            .split("\x1f")
        )
        expected = [
            self.config["server_pid"],
            self.config["server_started"],
            self.config["socket_path"],
            worker["pane_id"],
            worker["pane_pid"],
            worker["worker_id"],
            worker["task_id"],
            worker["run_id"],
            worker["pane_token"],
        ]
        if raw != expected:
            raise Unavailable("identity_mismatch")

    def inspect(self, worker_id, *, capture=True, now=None):
        now = now or datetime.now(timezone.utc)
        workers = [w for w in self.config["workers"] if w["worker_id"] == worker_id]
        if len(workers) != 1:
            raise Unavailable("unregistered_worker")
        w = workers[0]
        result = {
            "version": 1,
            "worker_id": worker_id,
            "task_id": w["task_id"],
            "run_id": w["run_id"],
            "role": w["role"],
            "host": self.config["host"],
            "server": {k: self.config[k] for k in ("socket_path", "server_pid", "server_started")},
            "checkout": w["checkout"],
            "observed_at": now.isoformat(),
            "identity": "unknown",
            "completion_verified": False,
            "authorization_granted": False,
            "terminal": None,
        }
        try:
            if self.config["host"] != socket.gethostname():
                raise Unavailable("host_mismatch")
            workspace = Path(self.config["workspace"]).resolve(strict=True)
            checkout = Path(w["checkout"]).resolve(strict=True)
            checkout.relative_to(workspace)

            def metadata():
                info = decode(self.upstream("info", "--pane", w["pane_id"]))
                if not isinstance(info, dict):
                    raise Unavailable("invalid_metadata")
                info = {k: text(info.get(k), 1024) for k in META}
                if info["pane_id"] != w["pane_id"]:
                    raise Unavailable("identity_mismatch")
                if (
                    not Path(info["pane_current_path"]).is_absolute()
                    or Path(info["pane_current_path"]).resolve(strict=True) != checkout
                    or Path(w["checkout"]).resolve(strict=True) != checkout
                    or Path(self.config["workspace"]).resolve(strict=True) != workspace
                    or not checkout.is_dir()
                ):
                    raise Unavailable("directory_mismatch")
                return info

            # Upstream can only discover all pane metadata. Filter immediately; never
            # capture or return other panes. Cap the entire discovery subprocess output.
            panes = decode(self.upstream("panes"))
            if (
                not isinstance(panes, list)
                or len(panes) > 512
                or any(not isinstance(p, dict) for p in panes)
            ):
                raise Unavailable("invalid_discovery")
            found = [p for p in panes if p.get("pane_id") == w["pane_id"]]
            if len(found) != 1:
                raise Unavailable("pane_missing_or_ambiguous")
            self.identity(w)
            info = metadata()
            terminal = (
                self.upstream("capture", "--pane", w["pane_id"], "--tail", "60")
                if capture
                else None
            )
            metadata()
            self.identity(w)
            result.update(identity="matched", pane=info, terminal=terminal)
        except (Unavailable, OSError, ValueError) as error:
            result["reason"] = (
                str(error) if isinstance(error, Unavailable) else "invalid_observation"
            )
        return result

    def status(self, observation, relative, now=None):
        result = {
            "valid": False,
            "input_delivery": "unknown",
            "acknowledgement": "unknown",
            "reported_completion": False,
            "completion_verified": False,
        }
        try:
            if observation["identity"] != "matched":
                raise Unavailable("identity_unproven")
            data = decode(workspace_bytes(self.config["workspace"], relative))
            required = {
                "version",
                "worker_id",
                "task_id",
                "run_id",
                "updated_at",
                "input_delivery",
                "acknowledged",
                "reported_completion",
                "commit",
                "command",
                "artifacts",
            }
            if (
                not isinstance(data, dict)
                or set(data) != required
                or type(data["version"]) is not int
                or data["version"] != 1
            ):
                raise Unavailable("status_schema")
            if any(data[k] != observation[k] for k in ("worker_id", "task_id", "run_id")):
                raise Unavailable("status_identity_mismatch")
            age = ((now or datetime.now(timezone.utc)) - stamp(data["updated_at"])).total_seconds()
            if not 0 <= age <= 300:
                raise Unavailable("status_stale_or_future")
            if data["input_delivery"] not in ("unknown", "reported_delivered") or any(
                type(data[k]) is not bool for k in ("acknowledged", "reported_completion")
            ):
                raise Unavailable("status_schema")
            if not isinstance(data["commit"], str) or not re.fullmatch(
                r"[0-9a-f]{40}", data["commit"]
            ):
                raise Unavailable("invalid_commit_reference")
            command = data["command"]
            if not isinstance(command, dict) or set(command) != {"argv", "exit_code"}:
                raise Unavailable("invalid_command_reference")
            if not isinstance(command["argv"], list) or not 1 <= len(command["argv"]) <= 32:
                raise Unavailable("invalid_command_reference")
            for arg in command["argv"]:
                text(arg, 1024)
            if type(command["exit_code"]) is not int or not -255 <= command["exit_code"] <= 255:
                raise Unavailable("invalid_exit_code")
            if not isinstance(data["artifacts"], list) or not 1 <= len(data["artifacts"]) <= 16:
                raise Unavailable("invalid_artifacts")
            for artifact in data["artifacts"]:
                if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                    raise Unavailable("invalid_artifact")
                raw = workspace_bytes(self.config["workspace"], text(artifact["path"], 1024))
                if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
                    raise Unavailable("artifact_hash_mismatch")
            result.update(
                valid=True,
                input_delivery=data["input_delivery"],
                acknowledgement="reported" if data["acknowledged"] else "unknown",
                reported_completion=data["reported_completion"],
                report=data,
                artifact_integrity="matched_worker_supplied_hashes",
            )
        except (ValueError, OSError, TypeError, KeyError) as error:
            result["reason"] = str(error) if isinstance(error, Unavailable) else "invalid_status"
        return result

    def assess(self, observation, *, opt_in=False, approved_digest=None):
        result = {
            "advisory_only": True,
            "completion_verified": False,
            "authorization_granted": False,
            "state": "disabled",
        }
        if not self.config.get("allow_external", False) or not opt_in:
            return result
        if observation.get("local_status", {}).get("valid"):
            return result | {"state": "local_status_preferred"}
        if observation.get("identity") != "matched" or observation.get("terminal") is None:
            return result | {"state": "unavailable"}
        payload = preview(observation)
        if approved_digest != hashlib.sha256(payload).hexdigest():
            return result | {"state": "preview_required"}
        try:
            answer = decode(self.upstream("assessment", external=True, payload=payload))
            if not isinstance(answer, dict) or answer.get("choice") not in STATES:
                raise Unavailable("invalid_assessment")
            values = answer.get("probabilities")
            scores = [answer.get("confidence")]
            if not isinstance(values, dict) or set(values) != STATES:
                raise Unavailable("invalid_assessment")
            scores += list(values.values())
            if (
                any(
                    type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1
                    for v in scores
                )
                or abs(sum(values.values()) - 1) > 0.03
            ):
                raise Unavailable("invalid_assessment")
            return result | {
                "state": "available",
                "choice": answer["choice"],
                "confidence": answer["confidence"],
                "probabilities": values,
            }
        except (Unavailable, ValueError, TypeError):
            return result | {"state": "provider_unavailable"}


def preview(observation):
    """Only selected metadata/text; status artifact contents are never exported."""
    data = {
        k: observation[k] for k in ("worker_id", "task_id", "run_id", "role", "pane", "terminal")
    }
    raw = json.dumps(data, ensure_ascii=True, sort_keys=True).encode()
    if len(raw) > 196608:
        raise Unavailable("assessment_payload_limit")
    return raw
