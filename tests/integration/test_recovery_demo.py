"""Recovery demonstrations (`recovery_demo.py`) asserted. SYNTHETIC data; real worker
and coordinator processes killed with SIGKILL; a synthetic exchange double."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recovery_demo  # noqa: E402


def test_every_recovery_path(tmp_path):
    out = recovery_demo.run(tmp_path)
    worker = out["pod_worker_sigkill"]
    assert worker["attempts"] == [["lost", "signal_9"], ["succeeded", "result"]]
    assert worker["model_sha256"] == worker["clean_model_sha256"]  # retry reproduces it
    assert worker["killed_pid_alive"] is False

    coordinator = out["coordinator_sigkill"]
    assert coordinator["worker_survived_coordinator_death"] is True
    assert coordinator["recovered_active_on_first_step"] == 1
    assert coordinator["attempts"] == [["succeeded", "result"]] and coordinator["results"] == 1
    assert coordinator["worker_alive_after"] is False

    crash = out["backtest_crash_resume"]
    assert crash["fills"] > 0 and len(crash["resumed"]) == 5
    assert all(
        r["crashed"] and r["identical"] and r["verification"] == [] for r in crash["resumed"]
    )

    live = out["live_adapter_unknown_submit"]
    assert live["submit_status"] == "unknown" and live["order_status_after_timeout"] == "unknown"
    assert live["same_intent_again_existing"] is True and live["reconciliation_clean"] is True
    assert live["resolved_status"] == "open"
    assert live["venue_orders_with_client_id"] == 1 and live["create_requests"] == 1
    assert live["ledger_verification"] == []
