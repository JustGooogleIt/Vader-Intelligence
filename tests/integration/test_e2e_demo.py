"""The deterministic end-to-end demonstration (`e2e_demo.py`) with its hand-checked
numbers asserted. SYNTHETIC data; real worker subprocesses; no network."""

import math
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_demo  # noqa: E402


def test_end_to_end_chain_with_hand_checked_numbers(tmp_path):
    out = e2e_demo.run(tmp_path)
    assert set(out["pods"]["tasks"].values()) == {"succeeded"}
    logistic = out["evaluator"]["logistic"]
    assert logistic["state"] == "validated" and logistic["paired_n"] == 240
    assert logistic["paired_delta"]["ci95"][1] < 0 and all(h < 0 for h in logistic["half_deltas"])
    assert logistic["proposals_on_snapshot"] == 2

    shadow = out["shadow"]
    p = shadow["model_probability"]
    assert shadow["status"] == "complete" and shadow["state"] == "shadow_tested"
    assert Decimal(shadow["model_brier"]).quantize(Decimal("0.000001")) == Decimal(
        shadow["expected_model_brier"]
    )
    assert math.isclose(float(shadow["model_log_loss"]), -math.log(p), abs_tol=1e-9)
    assert shadow["midpoint_brier"] == "0.126025" and shadow["midpoint_log_loss"] == "0.438505"
    delta = Decimal(shadow["paired_with_midpoint"]["mean_brier_delta"])
    assert math.isclose(float(delta), (p - 1) ** 2 - (0.645 - 1) ** 2, abs_tol=1e-9)
    # Independent of the evaluator: log-loss delta = (-ln p) - (-ln 0.645).
    log_delta = Decimal(shadow["paired_with_midpoint"]["mean_log_loss_delta"])
    assert math.isclose(float(log_delta), math.log(0.645) - math.log(p), abs_tol=1e-9)

    assert out["paper"]["state"] == "paper_approved" and out["paper"]["verification"] == []
    assert out["candidate"]["transitions"] == [
        "proposed",
        "validated",
        "shadow_tested",
        "paper_approved",
    ]
    assert out["holdout"]["registry_ledger"]["evaluations"] == 1
    assert out["holdout"]["second_attempt"].startswith("refused")

    assert out["dry_run"]["outcomes"] == ["submitted_dry"] and out["dry_run"]["requests"] == 2
    assert out["dry_run"]["url"].startswith("https://external-api.demo.kalshi.co/")
    assert out["dry_run"]["body"]["price"] == "0.6500"  # YES ask = 1 - NO bid 0.35

    arithmetic = out["execution_arithmetic"]
    assert arithmetic["order_limit"] == "0.61" and arithmetic["fills_without_depth"] == 0
    assert arithmetic["fill_prices"] == ["0.61"]
    assert Decimal(arithmetic["filled_quantity_with_depth_4"]) <= 2
    assert arithmetic["realized"] == arithmetic["expected_realized"]
    # Independent of the fee code: Kalshi general taker fee, rounded up to the cent,
    # ceil(100 x 0.07 x C x P x (1 - P)) / 100 for C = 2 contracts at P = 0.61.
    fee = Decimal(math.ceil(100 * 0.07 * 2 * 0.61 * 0.39)) / 100
    assert Decimal(arithmetic["fees"]) == fee == Decimal("0.04")
    assert Decimal(arithmetic["realized"]) == 2 * Decimal("0.39") - fee
