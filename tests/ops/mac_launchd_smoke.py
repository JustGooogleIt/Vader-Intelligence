"""Opt-in native launchd drill; only a random test label and disposable archive.

Run from repository root: .venv/bin/python tests/ops/mac_launchd_smoke.py
Never starts the public collector. Does not alter any production job.
"""

import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_operations import FAKE  # noqa: E402

from ops import launchd  # noqa: E402
from ops.common import label, read_state, write_json  # noqa: E402


def main():
    if sys.platform != "darwin":
        raise SystemExit("This opt-in drill requires macOS. No scheduler changes made.")
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="vader-ops-test-") as temporary:
        root = Path(temporary).resolve()
        state = root / "state"
        state.mkdir()
        package = root / "src/vader_intelligence"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "cli.py").write_text(FAKE)
        database = root / "test.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript(
                (repo / "src/vader_intelligence/migrations/001_initial.sql").read_text()
            )
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO schema_migrations VALUES (1,'test')")
        collector = root / "collector.toml"
        collector.write_text("[collector]\n")
        config = {
            "version": 1,
            "checkout": str(root),
            "python": sys.executable,
            "database": str(database),
            "collector_config": str(collector),
            "state": str(state),
            "cadence": 30,
            "deadline": 2,
            "grace": 1,
            "retention": 10,
            "log_bytes": 1024,
            "test_label": True,
        }
        path = root / "ops.json"
        write_json(path, config)
        write_json(
            state / "history.json",
            {
                "version": 1,
                "database": str(database),
                "sequence": 0,
                "history": [],
            },
        )
        definition = launchd.plist(config, path)
        definition["ThrottleInterval"] = 1
        definition["ProgramArguments"][1] = str(repo / "ops/manage.py")
        target = f"gui/{os.getuid()}/{label(config)}"
        plist_path = root / f"{label(config)}.plist"
        evidence = []
        for mode in ("stubborn", "empty"):
            definition["EnvironmentVariables"]["VADER_OPS_TEST_MODE"] = mode
            pid_file = root / "test-child.pid"
            definition["EnvironmentVariables"]["VADER_OPS_TEST_PID_FILE"] = str(pid_file)
            plist_path.write_bytes(plistlib.dumps(definition))
            subprocess.run(["/usr/bin/plutil", "-lint", str(plist_path)], check=True)
            prior = read_state(config)["sequence"]
            bootstrapped = False
            try:
                launchd.execute(
                    ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)]
                )
                bootstrapped = True
                deadline = time.monotonic() + 15
                duplicate = subprocess.run(
                    ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if duplicate.returncode == 0:
                    raise RuntimeError("launchd accepted a second bootstrap of the loaded label")
                if mode == "stubborn":
                    while not pid_file.exists():
                        if time.monotonic() > deadline:
                            raise TimeoutError("test child did not start")
                        time.sleep(0.02)
                    overlap = subprocess.run(
                        [sys.executable, str(repo / "ops/manage.py"), "--config", str(path), "run"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if (
                        overlap.returncode != 75
                        or json.loads(overlap.stdout)["status"] != "overlap_skipped"
                    ):
                        raise RuntimeError(f"native overlap was not rejected: {overlap}")
                while True:
                    data = read_state(config)
                    if data["sequence"] > prior and data["history"][-1]["ended_at"]:
                        entry = data["history"][-1]
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("test launchd invocation did not finish")
                    time.sleep(0.1)
                expected = (
                    "deadline_exceeded" if mode == "stubborn" else "discovery_no_eligible_games"
                )
                if entry["status"] != expected:
                    raise RuntimeError(f"expected {expected}: {entry}")
                if mode == "stubborn" and entry["collector_exit_code"] != -9:
                    raise RuntimeError("stubborn child was not forcibly terminated")
                expected_code = 124 if mode == "stubborn" else 3
                while launchd.loaded(config).get("last_exit_code") != expected_code:
                    if time.monotonic() > deadline:
                        raise TimeoutError("launchctl did not confirm wrapper exit")
                    time.sleep(0.05)
                if data["sequence"] != prior + 1:
                    raise RuntimeError("unexpected duplicate scheduled invocation")
                evidence.append(
                    {
                        **entry,
                        "launchctl_exit_verified": expected_code,
                        "overlap_verified": mode == "stubborn",
                        "duplicate_bootstrap_rejected": duplicate.returncode,
                    }
                )
            finally:
                if bootstrapped:
                    launchd.execute(["/bin/launchctl", "bootout", target])
                    launchd.drain(config)
                    if launchd.loaded(config)["loaded"]:
                        raise RuntimeError("test service remained loaded after bootout")
                    if mode == "stubborn":
                        try:
                            os.kill(int(pid_file.read_text()), 0)
                        except ProcessLookupError:
                            evidence[-1]["child_cleanup_verified"] = True
                        else:
                            raise RuntimeError("test child survived cleanup")
        print(
            json.dumps(
                {"test_label": label(config), "synthetic": True, "evidence": evidence}, indent=2
            )
        )


if __name__ == "__main__":
    main()
