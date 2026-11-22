"""Ledger operator CLI: no orders, no credentials; synthetic ledgers only."""

import pytest

from vader_intelligence.cli import execute, parser
from vader_intelligence.trading.ledger import EnvironmentMismatch


def cli(*args):
    return execute(parser().parse_args(list(args)))


def test_paper_ledger_init_show_verify_and_kill_switch(tmp_path):
    path = str(tmp_path / "paper.sqlite3")
    base = ["--ledger", path, "--environment", "paper"]
    cli("ledger", "init", *base, "--deposit", "1000")
    cli("ledger", "init", *base, "--deposit", "1000")  # re-running init adds no cash
    shown = cli("ledger", "show", *base)
    assert shown["cash"] == "1000" and shown["kill_switch"]["engaged"] is False
    assert shown["unknown_state"] == [] and shown["limits_hash"] is None
    assert cli("ledger", "verify", *base) == {"status": "complete", "failures": []}
    cli("ledger", "kill-switch", *base, "--engage", "--reason", "drill", "--actor", "operator")
    assert cli("ledger", "show", *base)["kill_switch"] == {
        "engaged": True,
        "reason": "drill",
        "default": False,
    }


def test_live_ledger_starts_engaged_refuses_deposits_and_reports_unset_limits(tmp_path):
    path = str(tmp_path / "live.sqlite3")
    base = ["--ledger", path, "--environment", "live"]
    with pytest.raises(ValueError, match="paper"):
        cli("ledger", "init", *base, "--deposit", "5")
    cli("ledger", "init", *base)
    shown = cli("ledger", "show", *base)
    assert shown["kill_switch"]["engaged"] is True and shown["kill_switch"]["default"] is True
    limits = tmp_path / "limits.toml"
    limits.write_text('[limits]\nmax_order_contracts = 1\nkelly_fraction = "0.05"\n')
    recorded = cli("ledger", "limits", *base, "--file", str(limits), "--actor", "operator")
    assert recorded["live_ready"] is False and "max_drawdown" in recorded["unset"]
    assert cli("ledger", "show", *base)["limits_hash"] == recorded["limits_hash"]
    with pytest.raises(EnvironmentMismatch):
        cli("ledger", "show", "--ledger", path, "--environment", "paper")


def test_missing_ledger_is_not_created_by_a_read(tmp_path):
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(ValueError, match="not found"):
        cli("ledger", "show", "--ledger", str(path), "--environment", "paper")
    assert not path.exists()
