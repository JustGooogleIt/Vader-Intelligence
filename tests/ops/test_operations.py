"""Offline stdlib suite: python -m unittest discover -s tests/ops -v.

Does not load the Unix-only collector or the shared pytest conftest on Windows.
Uses real SQLite WAL databases, locks, pipes and children. launchctl is simulated.
"""

import argparse
import json
import os
import plistlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ops import archive, launchd, manage, runner
from ops.common import command, digest, label, load, lock, read_state, validate, write_json

REPO = Path(__file__).resolve().parents[2]
FAKE = """import json, os, sqlite3, sys, time
mode = os.environ.get("VADER_OPS_TEST_MODE", "empty")
if mode == "hang":
    time.sleep(30)
if mode == "large":
    print("x" * 100000)
    sys.exit(0)
if mode == "invalid":
    print("not-json")
    sys.exit(0)
if mode == "stderr":
    print('{"status":"failed","error":"injected"}', file=sys.stderr)
    sys.exit(1)
database = sys.argv[sys.argv.index("--db") + 1]
db = sqlite3.connect(database)
run_id = str(time.time_ns())
status, code, books, eligible, errors = "inconclusive", 3, 0, 0, []
if mode == "books":
    status, code, books, eligible = "complete", 0, 2, 2
if mode == "cutoff":
    eligible = 1
if mode == "partial":
    status, code, errors = "partial", 1, ["injected request failure"]
if mode == "interrupted":
    status, code, errors = "interrupted", 130, ["interrupted"]
result = dict(run_id=run_id, status=status, errors=errors, books_this_pass=books)
db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
           (run_id,"collect","start","end",status,"{}",json.dumps(result)))
for i in range(eligible):
    db.execute("INSERT INTO eligibility(run_id,ticker,checked_at,eligible,reason,data_json) "
               "VALUES (?,?,?,1,?,?)",(run_id,str(i),"now","eligible","{}"))
db.commit()
db.close()
print(json.dumps(result))
sys.exit(code)
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vader ops & ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.db = self.root / "archive & observations.sqlite3"
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(
                (REPO / "src/vader_intelligence/migrations/001_initial.sql").read_text()
            )
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO schema_migrations VALUES (1,'now')")
        package = self.root / "src/vader_intelligence"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "cli.py").write_text(FAKE)
        self.collector_config = self.root / "collector & config.toml"
        self.collector_config.write_text("[collector]\n")
        self.state = self.root / "operations state"
        self.state.mkdir()
        self.config = {
            "version": 1,
            "checkout": str(self.root),
            "python": sys.executable,
            "database": str(self.db),
            "collector_config": str(self.collector_config),
            "state": str(self.state),
            "cadence": 120,
            "deadline": 5,
            "grace": 1,
            "retention": 2,
            "log_bytes": 1024,
            "test_label": True,
        }
        write_json(
            self.state / "history.json",
            {
                "version": 1,
                "database": str(self.db),
                "sequence": 0,
                "history": [],
            },
        )
        self.config_path = self.root / "operations config.json"
        write_json(self.config_path, self.config)

    def invoke(self, mode="empty"):
        with patch.dict(os.environ, {"VADER_OPS_TEST_MODE": mode}):
            return runner.run(self.config)


class ConfigurationTests(Fixture):
    def test_arguments_are_separate_and_global_options_precede_collect(self):
        argv = command(self.config)
        self.assertEqual(argv[-2:], ["collect", "--once"])
        self.assertEqual(argv[argv.index("--db") + 1], str(self.db))
        self.assertEqual(argv[argv.index("--config") + 1], str(self.collector_config))
        result, code = self.invoke()
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "discovery_no_eligible_games")

    def test_config_validation_and_digest(self):
        self.assertEqual(load(self.config_path, digest(self.config_path)), self.config)
        for field, value in (
            ("database", "relative.db"),
            ("deadline", 120),
            ("cadence", True),
            ("retention", 1001),
            ("log_bytes", 0),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(dict(self.config, **{field: value}))
        with self.assertRaises(ValueError):
            load(self.config_path, "wrong")

    def test_configure_exclusive_paths(self):
        args = argparse.Namespace(
            **dict(
                self.config,
                state=str(self.root / "new state"),
                output=str(self.root / "new config.json"),
            )
        )
        result = manage.configure(args)
        self.assertEqual(result["scheduler"], "not_installed")
        with self.assertRaises(FileExistsError):
            manage.configure(args)
        self.assertEqual(archive.inspect(self.db)["counts"]["runs"], 0)

    def test_plist_round_trip_and_test_label(self):
        value = plistlib.loads(plistlib.dumps(launchd.plist(self.config, self.config_path)))
        self.assertEqual(value["StartInterval"], 120)
        self.assertNotIn("KeepAlive", value)
        self.assertTrue(value["Label"].startswith("com.vader.test.collection."))
        self.assertNotEqual(label(self.config), label(dict(self.config, test_label=False)))
        self.assertIn(str(self.config_path), value["ProgramArguments"])
        self.assertIn(digest(self.config_path), value["ProgramArguments"])
        self.assertEqual(value["StandardErrorPath"], "/dev/null")

    def test_state_database_identity_and_hardlinks(self):
        with self.assertRaises(ValueError):
            read_state(dict(self.config, database=str(self.root / "other.sqlite3")))
        hardlink = self.root / "alias.sqlite3"
        os.link(self.db, hardlink)
        with self.assertRaises(ValueError):
            validate(self.config)


class RunnerTests(Fixture):
    def test_success_no_games_and_exit_codes(self):
        for mode, expected, status in (
            ("books", 0, "collected_eligible_games"),
            ("empty", 3, "discovery_no_eligible_games"),
            ("partial", 1, "failed_or_partial"),
            ("interrupted", 130, "failed_or_partial"),
            ("cutoff", 3, "inconclusive_with_eligible_games"),
        ):
            with self.subTest(mode=mode):
                result, code = self.invoke(mode)
                self.assertEqual((code, result["status"]), (expected, status))
                self.assertIsNotNone(result["ended_at"])
                self.assertGreater(result["runtime_seconds"], 0)
                self.assertTrue(Path(result["stdout"]).is_file())

    def test_failure_output_and_bounds(self):
        for mode, status in (
            ("invalid", "wrapper_failed"),
            ("stderr", "wrapper_failed"),
            ("large", "output_limit_exceeded"),
        ):
            result, code = self.invoke(mode)
            self.assertEqual(code, 1)
            self.assertEqual(result["status"], status)
            self.assertLessEqual(Path(result["stdout"]).stat().st_size, 1024)
        self.assertEqual(len(read_state(self.config)["history"]), 2)
        self.assertLessEqual(len(list(self.state.glob("*.log"))), 4)
        self.assertEqual(archive.inspect(self.db)["counts"]["runs"], 0)

    def test_overlap_real_separate_process_lock(self):
        lock_path = str(self.db) + ".ops-run.lockfile"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from ops.common import lock; import sys,time; "
                "\nwith lock(sys.argv[1]):\n print('ready',flush=True)\n time.sleep(30)",
                lock_path,
            ],
            cwd=REPO,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            result, code = self.invoke()
            self.assertEqual((result["status"], code), ("overlap_skipped", 75))
            self.assertTrue((self.state / "last-overlap.json").exists())
            self.assertEqual(read_state(self.config)["sequence"], 0)
        finally:
            process.kill()
            process.wait(timeout=5)
            process.stdout.close()
        self.assertEqual(self.invoke()[1], 3)  # crash releases lock without unlinking it

    def test_deadline_then_restart(self):
        self.config["deadline"] = 1
        result, code = self.invoke("hang")
        self.assertEqual((result["status"], code), ("deadline_exceeded", 124))
        self.assertLess(result["runtime_seconds"], 5)
        self.assertEqual(self.invoke()[1], 3)

    def test_interrupt_supervisor_then_restart(self):
        env = dict(os.environ, VADER_OPS_TEST_MODE="hang")
        process = subprocess.Popen(
            [
                sys.executable,
                str(REPO / "ops/manage.py"),
                "--config",
                str(self.config_path),
                "run",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        try:
            while not read_state(self.config)["history"]:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            if os.name == "nt":
                # Windows TerminateProcess cannot exercise POSIX signal forwarding.
                # The child's own 5-second deadline is handled by the wrapper instead.
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 124, stderr)
            else:
                time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, stderr)
            self.assertIsNotNone(json.loads(stdout)["ended_at"])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.assertEqual(self.invoke()[1], 3)

    def test_health_stale_empty_and_unknown_cause(self):
        self.assertEqual(runner.health(self.config)["status"], "never_invoked")
        result, _ = self.invoke()
        report = runner.health(self.config, now=result["start_epoch"] + 1)
        self.assertEqual(report["status"], "discovery_no_eligible_games")
        report = runner.health(self.config, now=result["start_epoch"] + 300)
        self.assertEqual(report["status"], "missed_or_stale")
        self.assertEqual(report["current_gap"]["cause"], "unknown")
        self.assertEqual(runner.gap(10, 9, 120, 5)["kind"], "clock_regression")

    def test_recovery_retains_unknown_completion_and_gap(self):
        data = read_state(self.config)
        data["history"] = [
            {
                "sequence": 0,
                "start_epoch": time.time() - 1000,
                "ended_at": None,
                "status": "running",
            }
        ]
        write_json(self.state / "history.json", data)
        result, _ = self.invoke()
        data = read_state(self.config)
        self.assertEqual(data["history"][0]["status"], "completion_unknown")
        self.assertEqual(result["gap_before"]["cause"], "unknown")

    def test_unverified_run_cannot_claim_success(self):
        self.assertEqual(
            archive.classify(
                self.config,
                {
                    "run_id": "missing",
                    "status": "inconclusive",
                    "errors": [],
                    "books_this_pass": 0,
                },
                3,
            ),
            "unverified_result",
        )
        self.assertEqual(archive.classify(self.config, [], 0), "failed_or_partial")


class ArchiveTests(Fixture):
    def test_live_wal_backup_restore_and_inspect(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO blobs VALUES ('evidence',?,?)", (b"research bytes", 14))
            db.commit()
            self.assertTrue(Path(str(self.db) + "-wal").exists())
            backup = self.root / "new backup.sqlite3"
            result = archive.backup(self.db, backup)
            self.assertEqual(result["integrity"], "ok")
            restored = self.root / "separate restore.sqlite3"
            result = archive.backup(backup, restored)
            self.assertEqual(result["counts"]["blobs"], 1)
            with closing(archive.connect(restored)) as test:
                self.assertEqual(
                    test.execute("SELECT body FROM blobs").fetchone()[0], b"research bytes"
                )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0], 1)

    def test_refuse_overwrite_and_newer_schema(self):
        before = digest(self.db)
        for path in (self.db, self.config_path):
            with self.assertRaises(FileExistsError):
                archive.backup(self.db, path)
        self.assertEqual(digest(self.db), before)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("PRAGMA user_version=2")
        with self.assertRaises(ValueError):
            archive.backup(self.db, self.root / "future.sqlite3")
        self.assertFalse((self.root / "future.sqlite3").exists())

    def test_missing_schema_corruption_and_timeout(self):
        with self.assertRaises(TimeoutError):
            archive.backup(self.db, self.root / "incomplete.sqlite3", timeout=-1)
        with self.assertRaises(FileExistsError):
            archive.backup(self.db, self.root / "incomplete.sqlite3")
        bad = self.root / "bad.sqlite3"
        bad.write_bytes(b"not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            archive.inspect(bad)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DROP TABLE eligibility")
        with self.assertRaises(ValueError):
            archive.inspect(self.db)


class LaunchdTests(Fixture):
    def mocks(self):
        path = self.root / (label(self.config) + ".plist")
        patches = [
            patch.object(launchd, "domain", return_value="gui/99999"),
            patch.object(launchd, "preflight", return_value={"test": True}),
            patch.object(launchd, "inventory", return_value=[]),
            patch.object(launchd, "installed_path", return_value=path),
            patch.object(launchd, "loaded", return_value={"loaded": False}),
            patch.object(launchd, "execute", return_value=""),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        return path, mocks

    def test_install_duplicate_stop_restart_uninstall(self):
        path, mocks = self.mocks()
        launchd.install(self.config, self.config_path)
        self.assertTrue(path.exists())
        with self.assertRaises(ValueError):
            launchd.install(dict(self.config, test_label=False), self.config_path)
        launchd.lifecycle(self.config, self.config_path, "stop")
        self.assertTrue(path.exists())
        launchd.lifecycle(self.config, self.config_path, "restart")
        self.assertTrue(any("bootstrap" in call.args[0] for call in mocks[-1].call_args_list))
        launchd.lifecycle(self.config, self.config_path, "uninstall")
        self.assertFalse(path.exists())
        self.assertTrue(self.db.exists())
        self.assertTrue((self.state / "history.json").exists())

    def test_conflicts_and_inspection_errors_fail_closed(self):
        path, mocks = self.mocks()
        mocks[2].return_value = [{"plist": "legacy.plist"}]
        with self.assertRaises(ValueError):
            launchd.install(self.config, self.config_path)
        self.assertFalse(path.exists())
        mocks[2].side_effect = PermissionError("cannot inspect services")
        with self.assertRaises(PermissionError):
            launchd.install(self.config, self.config_path)
        self.assertFalse(path.exists())

    def test_bootstrap_failure_preserves_claim_and_safe_uninstall(self):
        path, mocks = self.mocks()
        mocks[-1].side_effect = RuntimeError("bootstrap failure")
        with self.assertRaises(RuntimeError):
            launchd.install(self.config, self.config_path)
        self.assertTrue(path.exists())
        mocks[-1].side_effect = None
        launchd.lifecycle(self.config, self.config_path, "uninstall")
        self.assertFalse(path.exists())

    def test_changed_plist_never_removed(self):
        path, _ = self.mocks()
        launchd.install(self.config, self.config_path)
        path.write_bytes(plistlib.dumps({"Label": "unrelated"}))
        with self.assertRaises(ValueError):
            launchd.lifecycle(self.config, self.config_path, "uninstall")
        self.assertTrue(path.exists())

    def test_services_parser_and_detection(self):
        sample = (
            "gui/99999 = {\n services = {\n 0 - com.example.opaque\n 12 0 com.example.other\n }\n}"
        )
        self.assertEqual(launchd.services(sample), ["com.example.opaque", "com.example.other"])
        self.assertTrue(launchd.is_collector("python -m vader_intelligence.cli collect --once"))
        self.assertFalse(launchd.is_collector("ordinary unrelated service"))
        with self.assertRaises(ValueError):
            launchd.services("unknown format")

    def test_install_lock_contention(self):
        self.mocks()
        with lock(str(self.db) + ".ops-install.lockfile"):
            with self.assertRaises(BlockingIOError):
                launchd.install(self.config, self.config_path)


if __name__ == "__main__":
    unittest.main()
