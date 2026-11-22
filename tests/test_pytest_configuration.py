"""The documented `.venv/bin/pytest -q` collects the same tests as `python -m pytest -q`."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_console_entry_point_imports_the_top_level_ops_package():
    # The pytest console script does not put the working directory on sys.path; -P
    # reproduces that, so `ops` must not resolve to the tests/ops namespace directory.
    modules = [
        "tests/test_discovery_boundary.py",
        "tests/forecast_storage/test_storage.py",
        "tests/ops/test_operations.py",
    ]
    result = subprocess.run(
        [sys.executable, "-P", "-m", "pytest", "--collect-only", "-q"]
        + ["-p", "no:cacheprovider", *modules],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "error" not in result.stdout.splitlines()[-1]
