"""Verify native imports without any production or test platform shim."""

import subprocess
import sys


def test_imports_do_not_load_storage_network_or_cli():
    script = """
import sys
from vader_intelligence.forecast import baselines, policy, selection
from vader_intelligence.evaluation import scoring, settlement
for name in ('fcntl', 'sqlite3', 'httpx', 'vader_intelligence.storage',
             'vader_intelligence.cli', 'vader_intelligence.transport'):
    assert name not in sys.modules, name
assert str(baselines.constant()) == '0.5'
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
