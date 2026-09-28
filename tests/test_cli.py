import json
import subprocess
import sys

import pytest
from conftest import FIXTURE

from vader_intelligence.config import Config
from vader_intelligence.replay import import_fixture


def test_offline_cli_database_fixture_replay_and_health(tmp_path):
    command = [sys.executable, "-m", "vader_intelligence.cli", "--db", str(tmp_path / "cli.db")]
    for args in (["db-init"], ["replay", "--fixture", str(FIXTURE)], ["replay"]):
        result = subprocess.run(command + args, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "complete"
    health = subprocess.run(command + ["health"], capture_output=True, text=True, timeout=10)
    assert health.returncode == 1
    assert json.loads(health.stdout)["last_book_at"] is None


def test_replay_rejects_late_book_but_preserves_raw(store, config, fixture_data, tmp_path):
    fixture_data["responses"][-1]["retrieved_at"] = "2026-09-27T19:03:00+00:00"
    path = tmp_path / "late.json"
    path.write_text(json.dumps(fixture_data))
    with pytest.raises(ValueError, match="at or after pregame cutoff"):
        import_fixture(store, path, config)
    assert (
        store.db.execute("SELECT COUNT(*) FROM observations WHERE kind='book'").fetchone()[0] == 0
    )
    row = store.db.execute(
        "SELECT body_sha256,state FROM fetches ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row[0] is not None and row[1] == "parse_error"


@pytest.mark.parametrize(
    "values",
    [
        {"pregame_buffer": 119},
        {"schedule_max_age": 121},
        {"requests_per_second": 3},
        {"request_budget": 46},
        {"max_attempts": 4},
        {"max_pages": 1.5},
        {"read_timeout": float("nan")},
        {"connect_timeout": 0},
    ],
)
def test_configuration_cannot_relax_safety_contract(values):
    with pytest.raises(ValueError):
        Config(**values)
