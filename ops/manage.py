"""Run with an absolute Python path: python /stable/checkout/ops/manage.py --help."""

import argparse
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path

# Direct script execution works without packaging or shared CLI changes.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops import archive, launchd, runner  # noqa: E402
from ops.common import absolute, label, load, validate, write_json  # noqa: E402


def parser():
    p = argparse.ArgumentParser(
        description="Vader collection operations (separate from research CLI)"
    )
    p.add_argument("--config", help="absolute operations JSON path")
    p.add_argument("--config-sha256", help="expected operations config digest (launchd guard)")
    subs = p.add_subparsers(dest="action", required=True)
    configure = subs.add_parser("configure", help="create new config and dedicated state directory")
    for name in ("checkout", "python", "database", "collector-config", "state", "output"):
        configure.add_argument("--" + name, required=True)
    configure.add_argument("--cadence", type=int, default=120)
    configure.add_argument("--deadline", type=int, default=90)
    configure.add_argument("--grace", type=int, default=5)
    configure.add_argument("--retention", type=int, default=1000)
    configure.add_argument("--log-bytes", type=int, default=65536)
    configure.add_argument("--test-label", action="store_true")
    for action in ("run", "health", "status", "install", "uninstall", "stop", "restart", "plist"):
        subs.add_parser(action)
    for action in ("backup", "restore"):
        sub = subs.add_parser(action, help="SQLite backup API into a NEW path only")
        sub.add_argument("--source", required=True)
        sub.add_argument("--destination", required=True)
    sub = subs.add_parser("inspect")
    sub.add_argument("--database", required=True)
    return p


def configure(args):
    config = {
        name: getattr(args, name)
        for name in (
            "checkout",
            "python",
            "database",
            "collector_config",
            "state",
            "cadence",
            "deadline",
            "grace",
            "retention",
            "log_bytes",
            "test_label",
        )
    }
    for key in ("checkout", "database", "collector_config", "state"):
        config[key] = str(absolute(config[key]).resolve())
    # Do not resolve a venv interpreter symlink: doing so bypasses the environment on Mac.
    absolute(config["python"])
    config["version"] = 1
    validate(config)
    for name in ("python", "collector_config"):
        if not Path(config[name]).is_file():
            raise ValueError(f"{name} must exist")
    archive.inspect(config["database"])
    output = absolute(args.output)
    if output.exists():
        raise FileExistsError(output)
    state = Path(config["state"])
    state.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_json(
        state / "history.json",
        {
            "version": 1,
            "database": config["database"],
            "sequence": 0,
            "history": [],
        },
        new=True,
    )
    write_json(output, config, new=True)
    return {"config": str(output), "label": label(config), "scheduler": "not_installed"}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "configure":
            result = configure(args)
        elif args.action in ("backup", "restore"):
            result = archive.backup(args.source, args.destination)
        elif args.action == "inspect":
            result = archive.inspect(args.database)
        else:
            if not args.config:
                raise ValueError("--config is required")
            config = load(absolute(args.config), args.config_sha256)
            if args.action == "plist":
                sys.stdout.buffer.write(plistlib.dumps(launchd.plist(config, args.config)))
                return 0
            if args.action == "run":
                result, code = runner.run(config)
                print(json.dumps(result))
                return code
            if args.action in ("health", "status"):
                result = runner.health(config)
                if args.action == "status":
                    result["scheduler"] = (
                        launchd.loaded(config)
                        if sys.platform == "darwin"
                        else {
                            "loaded": None,
                            "detail": "not macOS; target Mac status unverified",
                        }
                    )
                print(json.dumps(result, indent=2))
                healthy = result["status"] in {
                    "collected_eligible_games",
                    "discovery_no_eligible_games",
                }
                if args.action == "status":
                    healthy = healthy and result["scheduler"]["loaded"] is True
                return 0 if healthy and not result["storage_pressure"] else 1
            if args.action == "install":
                result = launchd.install(config, args.config)
            else:
                result = launchd.lifecycle(config, args.config, args.action)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
