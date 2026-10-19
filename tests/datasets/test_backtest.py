"""Historical paper backtest (roadmap D/H) on the SYNTHETIC history archive.

Candle quotes on both sides of the cutoff drive execution, the archived result settles,
candles supply no depth unless the labelled scenario assumes some, and the holdout is
reachable only through a ledger-recorded run. Values are invented.
"""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from history import test_acquire as synthetic

from datasets.test_training import SPLIT as SYNTHETIC_SPLIT
from datasets.test_training import synthetic_dataset
from vader_intelligence.cli import execute, parser
from vader_intelligence.datasets import history as dataset_history
from vader_intelligence.datasets.splits import SplitDefinition
from vader_intelligence.history.acquire import Acquisition
from vader_intelligence.models.artifact import ModelArtifact
from vader_intelligence.research import backtest, training
from vader_intelligence.research.registry import Registry
from vader_intelligence.trading.simulator import ForecastInput, PaperConfig, run_paper

DAY = date(2026, 9, 20)
VALIDATION = SplitDefinition("2026-09-10", "2026-09-30", embargo_days=1)
HOLDOUT = SplitDefinition("2026-09-01", "2026-09-15", embargo_days=1)
NYM = "KXMLBGAME-26SEP201310PHINYM-NYM"


def archive(store, config):
    reader = synthetic.history_reader(store, config, synthetic.handler_factory([]))
    try:
        Acquisition(store, reader).run(start_date=DAY, through_date=DAY, post_cutoff_seconds=900)
    finally:
        reader.close()
    return dataset_history.build(
        store.db, feature_set="fs-market-v1", start_date=DAY, through_date=DAY, name="bt"
    )


def test_execution_inputs_use_candles_around_the_cutoff_only(store, config):
    _, rows = archive(store, config)
    execution = backtest.execution_inputs(store.db, rows)
    assert {m["ticker"] for m in execution["markets"]} == {r["ticker"] for r in rows}
    for row in rows:
        cutoff = datetime.fromisoformat(row["cutoff"])
        times = [
            datetime.fromisoformat(s["as_of"])
            for s in execution["snapshots"]
            if s["ticker"] == row["ticker"]
        ]
        assert times and all(
            cutoff - timedelta(seconds=120) <= t <= cutoff + timedelta(seconds=300) for t in times
        )
        assert any(t <= cutoff for t in times) and any(t > cutoff for t in times)
    nym = [s for s in execution["snapshots"] if s["ticker"] == NYM]
    assert {(s["yes_bid"], s["yes_ask"]) for s in nym} == {("0.6000", "0.6100")}
    assert {s["result"] for s in execution["settlements"]} <= {"yes", "no"}
    assert execution["skipped"] == {}


def test_hand_checked_fill_only_under_the_depth_scenario(store, config, tmp_path):
    _, rows = archive(store, config)
    execution = backtest.execution_inputs(store.db, rows)
    nym = next(r for r in rows if r["ticker"] == NYM)
    forecast = ForecastInput(
        forecast_id="hand:nym",
        ticker=NYM,
        probability=Decimal("0.75"),
        computed_at=datetime.fromisoformat(nym["cutoff"]),
        model_id="hand",
    )
    inputs = backtest.paper_inputs(execution, [forecast], "synthetic-history")
    no_depth = run_paper(PaperConfig(), inputs, tmp_path / "a.sqlite3").to_dict()
    assert len(no_depth["orders"]) == 1 and not no_depth["fills"]
    (order,) = no_depth["orders"]
    assert order["side"] == "yes" and order["limit_price"] == "0.61"  # YES ask 0.61
    assert no_depth["pnl"]["realized"] == "0"
    depth = run_paper(PaperConfig(assumed_depth=Decimal(4)), inputs, tmp_path / "b.sqlite3")
    report = depth.to_dict()
    assert report["fills"], report["rejections_by_reason"]
    quantity = sum(Decimal(f["quantity"]) for f in report["fills"])
    fees = sum(Decimal(f["fee"]) for f in report["fills"])
    assert all(Decimal(f["price"]) == Decimal("0.61") for f in report["fills"])
    assert quantity <= 2  # 50% haircut of the assumed 4 contracts at the best level
    # NYM won: each contract pays $1 - $0.61, less fees.
    assert Decimal(report["pnl"]["realized"]) == quantity * Decimal("0.39") - fees
    assert any("SCENARIO" in a for a in report["manifest"]["assumptions"])


def registered(store, config, tmp_path, split):
    manifest, rows = archive(store, config)
    registry = Registry(tmp_path / "research")
    dataset_id = registry.register_dataset(manifest, rows)
    synthetic_manifest, synthetic_rows = synthetic_dataset()
    synthetic_id = registry.register_dataset(synthetic_manifest, synthetic_rows)
    trained = training.train(
        registry, synthetic_id, family="logistic", split=SYNTHETIC_SPLIT, grid={"C": [1.0]}
    )
    artifact = ModelArtifact.from_dict(json.loads(registry.model_artifact(trained["model_id"])))
    moved = replace(
        artifact, training=artifact.training | {"dataset_id": dataset_id, "split": split.to_dict()}
    )
    model_id = registry.register_model(
        moved.to_json(), family="logistic", dataset_id=dataset_id, summary={"test": True}
    )
    return registry, dataset_id, model_id


def test_backtest_run_reports_the_segment_and_refuses_mismatches(store, config, tmp_path):
    registry, dataset_id, model_id = registered(store, config, tmp_path, VALIDATION)
    try:
        result = backtest.run(
            registry,
            store.db,
            dataset_id,
            model_id,
            VALIDATION,
            PaperConfig(),
            segment="validation",
            ledger_path=tmp_path / "run.sqlite3",
        )
        assert result["segment"] == "validation" and result["selection_biased"] is True
        assert result["rows"] == 2 and result["forecasts"] == 2
        assert result["period"] == {"start": "2026-09-20", "end": "2026-09-20"}
        assert result["report"]["manifest"]["simulated"] is True
        assert not result["report"]["fills"]  # candles supply no depth by default
        with pytest.raises(ValueError, match="split"):
            backtest.run(
                registry,
                store.db,
                dataset_id,
                model_id,
                HOLDOUT,
                PaperConfig(),
                ledger_path=tmp_path / "x.sqlite3",
            )
    finally:
        registry.close()


def test_holdout_backtest_is_recorded_in_the_holdout_ledger(store, config, tmp_path):
    registry, dataset_id, model_id = registered(store, config, tmp_path, HOLDOUT)
    try:
        assert registry.holdout_status(dataset_id, HOLDOUT.key)["untouched"] is True
        result = backtest.run(
            registry,
            store.db,
            dataset_id,
            model_id,
            HOLDOUT,
            PaperConfig(),
            segment="holdout",
            grid={
                "fee_schedules": (PaperConfig().fee_schedule,),
                "extra_slippage_ticks": (0,),
                "latency": (PaperConfig().latency,),
                "maker_fill_model": ("none",),
                "assumed_depth": (None, Decimal(4)),
            },
        )
        assert result["selection_biased"] is False
        assert result["holdout_ledger"]["evaluations"] == 1
        assert registry.holdout_status(dataset_id, HOLDOUT.key)["untouched"] is False
        rows = result["report"]["rows"]
        assert [r["scenario"]["assumed_depth"] for r in rows] == [None, "4"]
        assert rows[0]["fills"] == 0
    finally:
        registry.close()


def test_cli_refuses_an_unacknowledged_holdout(tmp_path):
    args = parser().parse_args(
        [
            "backtest",
            "run",
            "--registry",
            str(tmp_path / "r"),
            "--history-db",
            str(tmp_path / "h.sqlite3"),
            "--dataset",
            "d",
            "--model",
            "m",
            "--development-through",
            "2026-09-01",
            "--validation-through",
            "2026-09-15",
            "--segment",
            "holdout",
        ]
    )
    with pytest.raises(ValueError, match="acknowledge-holdout"):
        execute(args)
