"""The deterministic demo runs end to end and its hand-checked expectations hold."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_research_demo_runs_and_matches_expectations():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/integration/research_demo.py")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "=== forward-shadow ===" in result.stdout
    assert "Paired scores (n=1): Brier constant 0.250000 midpoint 0.126025" in result.stdout
    summary = json.loads(result.stdout[result.stdout.index('{\n  "status"') :])
    assert summary["status"] == "complete" and summary["synthetic"] is True
    assert all(check["ok"] for check in summary["checks"]) and len(summary["checks"]) >= 20
