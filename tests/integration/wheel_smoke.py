"""Installed-wheel CLI proof from outside the checkout, using disposable databases.

Every schema and record-contract transition is asserted explicitly, from the archive
itself (`PRAGMA user_version`, `schema_migrations`, payload contract versions), not
only from command output, so a check cannot claim a schema it did not exercise.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--python", required=True, help="absolute installed-wheel environment Python"
    )
    args = parser.parse_args()
    python = str(Path(args.python).absolute())
    repo = Path(__file__).resolve().parents[2]
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    evidence = []
    with tempfile.TemporaryDirectory(prefix="vader-wheel-smoke-") as temporary:
        root = Path(temporary).resolve()

        def invoke(argv, expected=0):
            result = subprocess.run(
                [python, *argv],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == expected, (argv, result.stdout, result.stderr)
            evidence.append({"argv": argv, "exit": result.returncode})
            return json.loads(result.stdout or result.stderr)

        def archive_facts(path):
            with sqlite3.connect(path) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                ledger = [
                    r[0]
                    for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")
                ]
                contracts = {}
                if version >= 3:
                    for table in ("forecast_receipts", "forecast_decisions"):
                        contracts[table] = dict(
                            db.execute(
                                f"SELECT COALESCE(json_extract(payload_json,'$.contract_version'),1), COUNT(*)"
                                f" FROM {table} GROUP BY 1"
                            ).fetchall()
                        )
            facts = {"user_version": version, "ledger": ledger, "contracts": contracts}
            evidence.append({"archive": str(path), **facts})
            return facts

        def assert_schema(path, version):
            facts = archive_facts(path)
            assert facts["user_version"] == version, facts
            assert facts["ledger"] == list(range(1, version + 1)), facts

        location = invoke(
            ["-c", "import json,vader_intelligence; print(json.dumps(vader_intelligence.__file__))"]
        )
        assert Path(location).is_relative_to(Path(python).parent.parent)
        database = root / "archive.sqlite3"
        core = ["-m", "vader_intelligence.cli", "--db", str(database)]
        assert invoke(core + ["db-init"])["schema_version"] == 1
        assert_schema(database, 1)
        invoke(core + ["replay", "--fixture", str(repo / "tests/fixtures/mlb.json")])
        invoke(core + ["replay"])
        assert invoke(core + ["settlement", "migrate"])["schema_version"] == 2
        assert_schema(database, 2)
        assert invoke(core + ["db-init"])["schema_version"] == 2
        invoke(core + ["settlement", "inspect"])
        invoke(core + ["settlement", "replay"])
        assert invoke(core + ["health"], 1)["status"] == "unhealthy"
        assert invoke(core + ["forecast", "migrate"])["schema_version"] == 3
        assert_schema(database, 3)
        assert invoke(core + ["forecast", "migrate"])["migrated"] is False
        assert_schema(database, 3)
        # Contract 2 is frozen (decision D-001): --record-contract 2 is refused on schema 3.
        # --record-contract 3 performs migrations 004 and 005 in one transaction; schema 5
        # is asserted from the archive.
        refused2 = invoke(core + ["forecast", "migrate", "--record-contract", "2"], 1)["error"]
        assert "frozen" in refused2, refused2
        assert_schema(database, 3)
        rc3 = invoke(core + ["forecast", "migrate", "--record-contract", "3"])
        assert rc3["schema_version"] == 5 and rc3["record_contract"] == 3 and rc3["migrated"]
        assert_schema(database, 5)
        assert invoke(core + ["forecast", "migrate", "--record-contract", "3"])["migrated"] is False
        assert_schema(database, 5)
        # A tick on an archive with no forward evidence is a recorded inconclusive (exit 3),
        # never a fabricated error; its witness receipts are contract 3 and synthetic
        # (the fixture run is synthetic lineage), with no forward receipt.
        tick = invoke(core + ["forecast", "tick", "--budget", "5"], expected=3)
        assert tick["status"] == "inconclusive" and tick["mode"] == "forward_shadow"
        facts = archive_facts(database)
        assert facts["contracts"]["forecast_receipts"] == {3: tick["witness"]["pre"]["receipts"]}
        with sqlite3.connect(database) as db:
            modes = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT json_extract(payload_json,'$.mode') FROM forecast_receipts"
                )
            }
            assert modes == {"synthetic"}, modes
            assert (
                db.execute("SELECT COUNT(*) FROM runs WHERE kind='forecast-witness'").fetchone()[0]
                >= 1
            )
        assert invoke(core + ["forecast", "inspect", "--limit", "5"])["count"] == 0
        # Reconstruction over an empty cohort, an evaluation of the empty forward
        # dataset (inconclusive) and the read-only inspections, all from the wheel.
        study = invoke(
            core
            + [
                "forecast",
                "reconstruct",
                "--dataset",
                "wheel-study",
                "--from",
                "2026-09-27",
                "--through",
                "2026-09-27",
            ],
            expected=3,
        )
        assert study["status"] == "inconclusive" and study["bindings_in_range"] == 0
        evaluation = invoke(
            core
            + [
                "evaluate",
                "--view",
                "forward-shadow",
                "--outcomes-as-of",
                "2026-09-28T00:00:00+00:00",
            ],
            expected=3,
        )
        assert evaluation["status"] == "inconclusive" and evaluation["evaluation_id"] == 1
        assert (
            evaluation["report"]["population"]["U"] == 0 and evaluation["mode"] == "forward_shadow"
        )
        listing = invoke(core + ["evaluation", "inspect"])
        assert listing["count"] == 1
        report = invoke(core + ["evaluation", "report", "--id", "1", "--format", "json"])
        assert report["report"]["population"]["U"] == 0
        research = invoke(
            core
            + [
                "research",
                "run",
                "--view",
                "forward-shadow",
                "--outcomes-as-of",
                "2026-09-28T00:00:00+00:00",
                "--from",
                "2026-09-27",
                "--through",
                "2026-09-27",
            ],
            expected=3,
        )
        assert research["status"] == "inconclusive"
        assert invoke(core + ["research", "inspect"])["count"] == 1
        assert invoke(core + ["db-init"])["schema_version"] == 5
        assert invoke(core + ["settlement", "migrate"])["schema_version"] == 5
        replayed = invoke(core + ["replay"])
        assert replayed["forecast_storage"]["replay"] == "preserved_not_reconstructed"
        assert (
            replayed["forecast_storage"]["record_contracts"]["forecast_receipts"]["3"]
            == facts["contracts"]["forecast_receipts"][3]
        )
        assert replayed["forecast_storage"]["record_contracts"]["evaluation_runs"] == {
            "1": 1,
            "2": 0,
            "3": 0,
            "4": 0,
        }
        assert_schema(database, 5)
        ops = [str(repo / "ops/manage.py")]
        backup, restored = root / "backup.sqlite3", root / "restored.sqlite3"
        invoke(ops + ["backup", "--source", str(database), "--destination", str(backup)])
        invoke(ops + ["restore", "--source", str(backup), "--destination", str(restored)])
        report = invoke(ops + ["inspect", "--database", str(restored)])
        assert report["schema_version"] == 5
        assert "forecast_publications" in report["counts"]
        assert report["research"]["record_contracts"]["forecast_receipts"] == {
            "1": 0,
            "2": 0,
            "3": facts["contracts"]["forecast_receipts"][3],
            "4": 0,
        }
        assert report["research"]["record_contracts"]["evaluation_runs"] == {
            "1": 1,
            "2": 0,
            "3": 0,
            "4": 0,
        }
        assert_schema(restored, 5)
        assert archive_facts(restored)["contracts"] == facts["contracts"]
        future_core = ["-m", "vader_intelligence.cli", "--db", str(restored)]
        with sqlite3.connect(restored) as db:
            db.execute("PRAGMA user_version=7")  # 1-6 are known; 7 is unknown
        assert "unsupported" in invoke(future_core + ["db-init"], 1)["error"]
        assert "supported" in invoke(ops + ["inspect", "--database", str(restored)], 1)["error"]
        # An invalid record contract in an archive is refused before any mutation.
        db = sqlite3.connect(restored, isolation_level=None)  # autocommit: no PRAGMA rollback
        db.execute("PRAGMA user_version=5")
        db.execute("DROP TRIGGER forecast_receipts_no_update")
        raw = json.loads(
            db.execute("SELECT payload_json FROM forecast_receipts WHERE id=1").fetchone()[0]
        )
        raw["contract_version"] = 99
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        db.execute("UPDATE forecast_receipts SET payload_json=? WHERE id=1", (encoded,))
        db.execute(
            "CREATE TRIGGER forecast_receipts_no_update BEFORE UPDATE ON forecast_receipts"
            " BEGIN SELECT RAISE(ABORT,'immutable forecast history'); END"
        )
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        db.close()
        assert_schema(restored, 5)
        before = restored.read_bytes()
        refused = invoke(future_core + ["db-init"], 1)["error"]
        assert "unknown record contract version 99" in refused, refused
        assert restored.read_bytes() == before
        with sqlite3.connect(restored) as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        # A schema-4 archive written by the draft code (PR #11 head) is opened, migrated
        # and backed up by the installed wheel; its legacy rows stay legacy (D-001).
        legacy = root / "legacy-draft.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.executescript(
                (repo / "tests/fixtures/legacy_archives/draft-5811991-schema4.sql").read_text()
            )
        assert_schema(legacy, 4)
        legacy_core = ["-m", "vader_intelligence.cli", "--db", str(legacy)]
        assert invoke(legacy_core + ["forecast", "migrate", "--record-contract", "3"])["migrated"]
        assert_schema(legacy, 5)
        legacy_replay = invoke(legacy_core + ["replay"])["forecast_storage"]
        assert legacy_replay["legacy_rows"] == 25, legacy_replay
        assert legacy_replay["record_contracts"]["forecast_receipts"] == {
            "1": 0,
            "2": 22,
            "3": 0,
            "4": 0,
        }
        invoke(ops + ["backup", "--source", str(legacy), "--destination", str(root / "lb.sqlite3")])
    print(
        json.dumps(
            {
                "status": "complete",
                "synthetic": True,
                "installed_module": location,
                "schema_versions_exercised": [1, 2, 3, 4, 5],
                "record_contracts_exercised": [1, 2, 3],
                "legacy_archives_exercised": ["draft-5811991-schema4"],
                "checks": evidence,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
