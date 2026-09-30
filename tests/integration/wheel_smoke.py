"""Installed-wheel CLI proof from outside the checkout, using disposable databases."""

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
                timeout=30,
            )
            assert result.returncode == expected, (argv, result.stdout, result.stderr)
            evidence.append({"argv": argv, "exit": result.returncode})
            return json.loads(result.stdout or result.stderr)

        location = invoke(
            ["-c", "import json,vader_intelligence; print(json.dumps(vader_intelligence.__file__))"]
        )
        assert Path(location).is_relative_to(Path(python).parent.parent)
        database = root / "archive.sqlite3"
        core = ["-m", "vader_intelligence.cli", "--db", str(database)]
        assert invoke(core + ["db-init"])["schema_version"] == 1
        invoke(core + ["replay", "--fixture", str(repo / "tests/fixtures/mlb.json")])
        invoke(core + ["replay"])
        assert invoke(core + ["settlement", "migrate"])["schema_version"] == 2
        assert invoke(core + ["db-init"])["schema_version"] == 2
        invoke(core + ["settlement", "inspect"])
        invoke(core + ["settlement", "replay"])
        assert invoke(core + ["health"], 1)["status"] == "unhealthy"
        ops = [str(repo / "ops/manage.py")]
        backup, restored = root / "backup.sqlite3", root / "restored.sqlite3"
        invoke(ops + ["backup", "--source", str(database), "--destination", str(backup)])
        invoke(ops + ["restore", "--source", str(backup), "--destination", str(restored)])
        assert invoke(ops + ["inspect", "--database", str(restored)])["schema_version"] == 2
        future_core = ["-m", "vader_intelligence.cli", "--db", str(restored)]
        with sqlite3.connect(restored) as db:
            db.execute("PRAGMA user_version=3")
        assert "unsupported" in invoke(future_core + ["db-init"], 1)["error"]
        assert "supported" in invoke(ops + ["inspect", "--database", str(restored)], 1)["error"]
    print(
        json.dumps(
            {
                "status": "complete",
                "synthetic": True,
                "installed_module": location,
                "checks": evidence,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
