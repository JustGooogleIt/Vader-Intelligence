"""Native install/active-stop/restart drill with real preflight and no provider calls.

Creates a disposable standalone Git checkout next to this checkout (not in /tmp),
commits an explicitly synthetic CLI there, and installs only its unique test label.
Run from the integration checkout: .venv/bin/python tests/ops/mac_lifecycle_smoke.py
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from test_operations import FAKE

REPO = Path(__file__).resolve().parents[2]

# Importing this CLI for real preflight does nothing. Only execution injects a
# synthetic workload, in a separate committed checkout, never production code.
STUB = """import fcntl, json, os, signal, subprocess, sys, time
from pathlib import Path

def main():
    database = Path(sys.argv[sys.argv.index("--db") + 1])
    root = database.parent
    with open(str(database) + ".lockfile", "a") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mode = (root / "mode").read_text()
        with (root / "invocations.jsonl").open("a") as output:
            output.write(json.dumps({"pid": os.getpid(), "mode": mode}) + "\\n")
            output.flush()
            os.fsync(output.fileno())
        if mode == "stubborn":
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            child = subprocess.Popen([sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(300)"],
                pass_fds=(writer.fileno(),))
            (root / "child.json").write_text(json.dumps({
                "pid": os.getpid(), "pgid": os.getpgrp(), "descendant": child.pid}))
            time.sleep(300)
        os.environ["VADER_OPS_TEST_MODE"] = "empty"
        exec(EMPTY, {"__name__": "__main__"})

EMPTY = REPLACE_EMPTY
if __name__ == "__main__":
    main()
"""


def command(argv, *, check=True, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=60, **kwargs)
    if check and result.returncode:
        raise RuntimeError(f"{argv}: {result.returncode}: {result.stderr}")
    return result


def wait_for(predicate, description, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.05)
    raise TimeoutError(description)


def gone(pid):
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def main():
    if sys.platform != "darwin":
        raise SystemExit("Requires native macOS; no resources created")
    root = Path(tempfile.mkdtemp(prefix="vader-lifecycle-drill-", dir=REPO.parent)).resolve()
    checkout = root / "checkout"
    config_path = root / "ops.json"
    data = root / "data"
    data.mkdir()
    config = None
    cleanup_verified = False
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "synthetic": True}
    try:
        command(["git", "clone", "--no-hardlinks", str(REPO), str(checkout)])
        command(
            ["git", "checkout", "--detach", "9d3d1ea2f167d5154dd6c67b3f47f2c7640fced7"],
            cwd=checkout,
        )
        # Use the exact operations implementation under test, including uncommitted fixes.
        for source in (REPO / "ops").glob("*.py"):
            shutil.copyfile(source, checkout / "ops" / source.name)
        (checkout / "src/vader_intelligence/cli.py").write_text(
            STUB.replace("REPLACE_EMPTY", repr(FAKE))
        )
        command(["git", "add", "ops", "src/vader_intelligence/cli.py"], cwd=checkout)
        command(
            [
                "git",
                "-c",
                "user.name=Vader synthetic drill",
                "-c",
                "user.email=drill@example.invalid",
                "commit",
                "-m",
                "test: isolated synthetic lifecycle workload",
            ],
            cwd=checkout,
        )
        command([sys.executable, "-m", "venv", "--without-pip", str(checkout / ".venv")])
        python = str(checkout / ".venv/bin/python")
        database = data / "synthetic.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript(
                (REPO / "src/vader_intelligence/migrations/001_initial.sql").read_text()
            )
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO schema_migrations VALUES (1,'synthetic')")
        collector = root / "collector.toml"
        collector.write_text("[collector]\n")
        (data / "mode").write_text("stubborn")
        manage = [python, str(checkout / "ops/manage.py")]
        configured = command(
            manage
            + [
                "configure",
                "--checkout",
                str(checkout),
                "--python",
                python,
                "--database",
                str(database),
                "--collector-config",
                str(collector),
                "--state",
                str(root / "state"),
                "--output",
                str(config_path),
                "--test-label",
                "--cadence",
                "120",
                "--deadline",
                "60",
                "--grace",
                "2",
            ]
        )
        config = json.loads(config_path.read_text())
        label = json.loads(configured.stdout)["label"]
        evidence.update(label=label, checkout=str(checkout))
        control = manage + ["--config", str(config_path)]
        installed = json.loads(command(control + ["install"]).stdout)
        evidence["preflight"] = installed["registration"]["preflight"]
        wait_for((data / "child.json").exists, "stubborn child did not start")
        child = json.loads((data / "child.json").read_text())
        assert not gone(child["pid"]) and not gone(child["descendant"])
        duplicate = command(control + ["install"], check=False)
        assert (
            duplicate.returncode == 1
            and "already has an operations registration" in duplicate.stderr
        )
        overlap = command(control + ["run"], check=False)
        assert overlap.returncode == 75
        assert json.loads(overlap.stdout)["status"] == "overlap_skipped"
        # Exercise helper bootout while both stubborn processes and writer lock are active.
        start = time.monotonic()
        stopped = json.loads(command(control + ["stop"]).stdout)
        assert stopped["scheduler"]["loaded"] is False
        wait_for(
            lambda: gone(child["pid"]) and gone(child["descendant"]), "child group survived stop"
        )
        try:
            os.killpg(child["pgid"], 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("child process group survived stop")
        # Both locks must be acquirable from a separate process after helper stop.
        lock_probe = (
            "from pathlib import Path; from ops.common import lock,load,adjacent; import sys; "
            "c=load(sys.argv[1]); "
            "a=lock(adjacent(c,'run.lockfile')); b=lock(Path(c['database']+'.lockfile')); "
            "a.__enter__(); b.__enter__(); b.__exit__(None,None,None); a.__exit__(None,None,None)"
        )
        command([python, "-c", lock_probe, str(config_path)], cwd=checkout)
        evidence["active_stop"] = {
            "seconds": time.monotonic() - start,
            "child": child,
            "group_gone": True,
            "both_locks_drained": True,
            "duplicate_install_exit": duplicate.returncode,
            "overlap_exit": overlap.returncode,
        }
        history_path = root / "state/history.json"
        stopped_history = json.loads(history_path.read_text())
        assert stopped_history["sequence"] == 1
        assert stopped_history["history"][-1]["status"] == "interrupted"
        assert stopped_history["history"][-1]["collector_exit_code"] == -9
        evidence["stopped_invocation"] = stopped_history["history"][-1]
        (data / "mode").write_text("empty")
        restarted = json.loads(command(control + ["restart"]).stdout)
        assert restarted["scheduler"]["loaded"] is True

        def completed():
            state = json.loads(history_path.read_text())
            return state if state["sequence"] == 2 and state["history"][-1]["ended_at"] else None

        state = wait_for(completed, "restart did not complete one invocation")
        assert state["history"][-1]["status"] == "discovery_no_eligible_games"
        evidence["restarted_invocation"] = state["history"][-1]
        command(control + ["stop"])
        state = json.loads(history_path.read_text())
        invocations = [
            json.loads(line) for line in (data / "invocations.jsonl").read_text().splitlines()
        ]
        assert state["sequence"] == len(invocations) == 2
        assert [item["mode"] for item in invocations] == ["stubborn", "empty"]
        evidence["invocations"] = invocations
        uninstalled = json.loads(command(control + ["uninstall"]).stdout)
        assert uninstalled["scheduler"]["loaded"] is False
        assert not Path(installed["registration"]["plist"]).exists()
        assert not Path(str(database) + ".ops-registration.json").exists()
        assert database.exists()
        cleanup_verified = True
        evidence["cleanup_verified"] = True
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(evidence, indent=2))
    finally:
        if config and not cleanup_verified:
            # Normal owned helper path only. On cleanup failure retain files for diagnosis.
            cleanup = command(
                [
                    config["python"],
                    str(checkout / "ops/manage.py"),
                    "--config",
                    str(config_path),
                    "uninstall",
                ],
                check=False,
            )
            cleanup_verified = cleanup.returncode == 0
            if not cleanup_verified:
                print(
                    f"Cleanup requires inspection of disposable {root}: {cleanup.stderr}",
                    file=sys.stderr,
                )
        if cleanup_verified or config is None:
            shutil.rmtree(root)


if __name__ == "__main__":
    main()
