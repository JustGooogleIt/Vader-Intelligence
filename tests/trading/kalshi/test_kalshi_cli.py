"""`vader live readiness`: local gate checks only; never loads the key or contacts the
exchange (the root conftest forbids network; the key file is deleted to prove it)."""

from datetime import datetime, timezone

from vader_intelligence.cli import execute, parser

from .test_kalshi_activation import Setup


def readiness(setup, ack):
    return execute(
        parser().parse_args(
            [
                "live",
                "readiness",
                "--activation-config",
                str(setup.config),
                "--acknowledge",
                ack,
                "--ledger",
                str(setup.ledger.path),
            ]
        )
    )


def test_readiness_passes_every_local_gate_without_touching_the_credential(tmp_path):
    setup = Setup(tmp_path)
    # The fixture disengages at synthetic (future) time; readiness uses the wall clock, so
    # record the operator's disengage at a real past instant.
    setup.ledger.set_kill_switch(
        False, "readiness drill", "operator", datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    ack = setup.write()
    setup.key_path.unlink()  # readiness must not need (or read) the key
    result = readiness(setup, ack)
    assert result["status"] == "complete" and result["ready_except_credential"] is True
    assert result["credential_checked"] is False
    assert "secret_loads" not in {g["gate"] for g in result["gates"]}


def test_readiness_reports_each_failed_gate(tmp_path):
    setup = Setup(tmp_path)
    setup.write(enabled="false")
    wrong = "0" * 64
    result = readiness(setup, wrong)
    failed = {g["gate"] for g in result["gates"] if not g["passed"]}
    assert result["status"] == "failed" and {"acknowledge_hash", "enabled"} <= failed
