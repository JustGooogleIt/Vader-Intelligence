"""Fail-closed launchd lifecycle. Never replace an unrelated job or plist."""

import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ops.archive import inspect
from ops.common import (
    adjacent,
    digest,
    environment,
    label,
    lock,
    sync_directory,
    write_json,
)

BASELINE = "f0b0419ba45d1aeeb4ca663f6274981df09397d7"


def plist(config, config_path):
    return {
        "Label": label(config),
        "ProgramArguments": [
            config["python"],
            str(Path(config["checkout"]) / "ops" / "manage.py"),
            "--config",
            str(Path(config_path).resolve()),
            "--config-sha256",
            digest(config_path),
            "run",
        ],
        "WorkingDirectory": config["checkout"],
        "StartInterval": config["cadence"],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "ExitTimeOut": config["grace"] + 5,
        "ThrottleInterval": config["cadence"],
        # The runner owns bounded logs. No append-only launchd log files.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def execute(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30, **kwargs)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {argv}: {result.stderr[:1000]}")
    return result.stdout


def preflight(config):
    checkout = Path(config["checkout"])
    if not (checkout / ".git").is_dir():
        raise ValueError("production must use a stable standalone checkout, not a worktree")
    if checkout.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("production checkout must not be in a temporary directory")
    if Path(__file__).resolve() != checkout / "ops" / "launchd.py":
        raise ValueError("run installation from the configured stable checkout's ops/manage.py")
    head = execute(["/usr/bin/git", "-C", str(checkout), "rev-parse", "HEAD"]).strip()
    execute(["/usr/bin/git", "-C", str(checkout), "merge-base", "--is-ancestor", BASELINE, "HEAD"])
    dirty = execute(
        ["/usr/bin/git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"]
    )
    if dirty.strip():
        raise ValueError(
            "stable checkout has tracked changes; commit/review them before deployment"
        )
    script = (
        "import json,pathlib,sys; import vader_intelligence.cli as cli; "
        "from vader_intelligence.config import Config; "
        "c=Config.load(sys.argv[1],sys.argv[2]); "
        "print(json.dumps({'cli':str(pathlib.Path(cli.__file__).resolve()),"
        "'python':sys.version,'config':c.__dict__}))"
    )
    proof = json.loads(
        execute(
            [config["python"], "-c", script, config["collector_config"], config["database"]],
            cwd=checkout,
            env=environment(config),
        )
    )
    if Path(proof["cli"]) != checkout / "src" / "vader_intelligence" / "cli.py":
        raise ValueError("interpreter imports a different collector checkout")
    proof.update(checkout_commit=head, database=inspect(config["database"]))
    return proof


def services(text):
    """Parse the services section of launchctl print, refusing an unknown layout."""
    match = re.search(r"(?m)^\s*services = \{\s*$([\s\S]*?)^\s*\}", text)
    if not match:
        raise ValueError("cannot inspect launchctl services section")
    return [line.split()[-1] for line in match.group(1).splitlines() if line.strip()]


def is_collector(text):
    lowered = text.lower()
    return "vader" in lowered or ("collect" in lowered and "--once" in lowered)


def inventory():
    """Inspect saved definitions and every readable loaded job, including opaque labels."""
    conflicts = []
    roots = [
        Path.home() / "Library/LaunchAgents",
        Path("/Library/LaunchAgents"),
        Path("/Library/LaunchDaemons"),
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.plist"):
            with path.open("rb") as stream:
                value = plistlib.load(stream)
            if is_collector(str(value)):
                conflicts.append({"plist": str(path), "label": value.get("Label")})
    for domain in (f"gui/{os.getuid()}", f"user/{os.getuid()}", "system"):
        listing = execute(["/bin/launchctl", "print", domain])
        for name in services(listing):
            detail = execute(["/bin/launchctl", "print", f"{domain}/{name}"])
            if is_collector(detail):
                conflicts.append({"loaded": f"{domain}/{name}"})
    cron = subprocess.run(["/usr/bin/crontab", "-l"], capture_output=True, text=True, timeout=10)
    if cron.returncode and "no crontab" not in cron.stderr.lower():
        raise RuntimeError("could not inspect current user's crontab")
    if is_collector(cron.stdout):
        conflicts.append({"crontab": "possible existing collector"})
    return conflicts


def domain():
    if sys.platform != "darwin":
        raise RuntimeError("launchd lifecycle requires macOS; no service was changed")
    return f"gui/{os.getuid()}"


def installed_path(config):
    return Path.home() / "Library" / "LaunchAgents" / f"{label(config)}.plist"


def loaded(config):
    result = subprocess.run(
        ["/bin/launchctl", "print", f"{domain()}/{label(config)}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return {"loaded": True, "detail": result.stdout}
    if "could not find service" in result.stderr.lower():
        return {"loaded": False, "detail": result.stderr}
    raise RuntimeError(f"launchctl status unknown: {result.stderr}")


def install(config, config_path):
    target_domain = domain()
    with lock(adjacent(config, "install.lockfile")):
        # Claim persists while stopped, through crashes and until explicit uninstall.
        claim = adjacent(config, "registration.json")
        if claim.exists():
            raise ValueError(f"database already has an operations registration: {claim}")
        proof = preflight(config)
        conflicts = inventory()
        if conflicts:
            raise ValueError(
                f"existing or ambiguous collector jobs; inspect before installation: {conflicts}"
            )
        path = installed_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = plistlib.dumps(plist(config, config_path))
        registration = {
            "config": str(Path(config_path).resolve()),
            "label": label(config),
            "plist": str(path),
            "preflight": proof,
            "config_sha256": digest(config_path),
            "plist_sha256": hashlib.sha256(payload).hexdigest(),
        }
        write_json(claim, registration, new=True)
        # Preserve both artifacts after failure for explicit diagnosis/uninstall.
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(path.parent)
        execute(["/bin/launchctl", "enable", f"{target_domain}/{label(config)}"])
        execute(["/bin/launchctl", "bootstrap", target_domain, str(path)])
        return {"status": "installed", "registration": registration, "scheduler": loaded(config)}


def owned(config, config_path):
    claim = adjacent(config, "registration.json")
    record = json.loads(claim.read_text(encoding="utf-8"))
    path = installed_path(config)
    if (
        record["config"] != str(Path(config_path).resolve())
        or record["label"] != label(config)
        or record["plist"] != str(path)
    ):
        raise ValueError("registration is owned by another configuration")
    if path.exists() and digest(path) != record["plist_sha256"]:
        raise ValueError("installed plist changed; refusing to stop/remove an unverified job")
    return claim, path


def drain(config):
    deadline = time.monotonic() + config["deadline"] + 2 * config["grace"]
    while True:
        try:
            with lock(adjacent(config, "run.lockfile")):
                # Also check the existing collector lock, including an orphaned child.
                with lock(Path(config["database"] + ".lockfile")):
                    return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("collector still holds a lock; registration retained")
            time.sleep(0.1)


def lifecycle(config, config_path, action):
    target_domain = domain()
    with lock(adjacent(config, "install.lockfile")):
        claim, path = owned(config, config_path)
        execute(["/bin/launchctl", "disable", f"{target_domain}/{label(config)}"])
        if loaded(config)["loaded"]:
            execute(["/bin/launchctl", "bootout", f"{target_domain}/{label(config)}"])
        drain(config)
        if action == "restart":
            record = json.loads(claim.read_text(encoding="utf-8"))
            if digest(config_path) != record["config_sha256"]:
                raise ValueError("configuration changed; uninstall then install the new version")
            preflight(config)
            conflicts = [item for item in inventory() if item.get("plist") != str(path)]
            if conflicts:
                raise ValueError(f"conflicting service detected: {conflicts}")
            execute(["/bin/launchctl", "enable", f"{target_domain}/{label(config)}"])
            execute(["/bin/launchctl", "bootstrap", target_domain, str(path)])
        elif action == "uninstall":
            if path.exists():
                path.unlink()
                sync_directory(path.parent)
            claim.unlink()
            sync_directory(claim.parent)
        return {"status": action, "scheduler": loaded(config), "archive_preserved": True}
