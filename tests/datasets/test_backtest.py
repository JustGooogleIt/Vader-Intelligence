"""Historical paper backtest (roadmap D/H) on the SYNTHETIC history archive.

Candle quotes on both sides of the cutoff drive execution, the archived result settles,
candles supply no depth unless the labelled scenario assumes some, and the holdout is
reachable only through a ledger-recorded run. Values are invented.
"""

import fcntl
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
from vader_intelligence.datasets import splits
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
    grid = {
        "fee_schedules": (PaperConfig().fee_schedule,),
        "extra_slippage_ticks": (0,),
        "latency": (PaperConfig().latency,),
        "maker_fill_model": ("none",),
        "assumed_depth": (None, Decimal(4)),
    }
    try:
        key = splits.holdout_key(registry, dataset_id, HOLDOUT)  # pins at first use
        assert key == "from=2026-09-17" and registry.holdout_status(dataset_id, key)["untouched"]
        ledger = tmp_path / "again.sqlite3"
        with pytest.raises(ValueError, match="insufficient holdout rows"):
            backtest.run(
                registry, store.db, dataset_id, model_id, HOLDOUT, PaperConfig(),
                segment="holdout", ledger_path=ledger, acknowledge_holdout=True,
            )  # fmt: skip
        assert not ledger.exists()
        assert registry.holdout_status(dataset_id, key)["untouched"]  # the floor consumes nothing
        result = backtest.run(
            registry,
            store.db,
            dataset_id,
            model_id,
            HOLDOUT,
            PaperConfig(),
            segment="holdout",
            grid=grid,
            min_holdout_rows=1,  # the SYNTHETIC archive has two holdout rows
            acknowledge_holdout=True,
        )
        assert result["selection_biased"] is False
        assert result["holdout_ledger"]["evaluations"] == 1
        assert registry.holdout_status(dataset_id, key)["untouched"] is False
        rows = result["report"]["rows"]
        assert [r["scenario"]["assumed_depth"] for r in rows] == [None, "4"]
        assert rows[0]["fills"] == 0
        # A second use by the same model is refused before anything is written.
        with pytest.raises(ValueError, match="already evaluated"):
            backtest.run(
                registry,
                store.db,
                dataset_id,
                model_id,
                HOLDOUT,
                PaperConfig(),
                segment="holdout",
                ledger_path=ledger,
                min_holdout_rows=1,
                acknowledge_holdout=True,
            )
        assert not ledger.exists()
    finally:
        registry.close()


def test_splits_reaching_the_pinned_holdout_are_refused(store, config, tmp_path):
    registry, dataset_id, model_id = registered(store, config, tmp_path, VALIDATION)
    try:
        assert registry.holdout_from(dataset_id) is None
        backtest.run(
            registry, store.db, dataset_id, model_id, VALIDATION, PaperConfig(),
            ledger_path=tmp_path / "v.sqlite3",
        )  # fmt: skip
        assert registry.holdout_from(dataset_id) == VALIDATION.first_holdout_date
        late = SplitDefinition("2026-09-15", "2026-10-31", embargo_days=1)
        with pytest.raises(ValueError, match="reaches the pinned holdout"):
            splits.guard(registry, dataset_id, late, pin_if_missing=True)
        with pytest.raises(ValueError, match="already pinned"):
            registry.pin_holdout(dataset_id, "2026-11-01", "operator")
        with pytest.raises(ValueError, match="holdout is pinned from"):
            splits.holdout_key(registry, dataset_id, HOLDOUT)  # holds out from another date
    finally:
        registry.close()


@pytest.mark.parametrize("spelling", ["20260901", "2026-W36-2"])
def test_a_holdout_pin_is_a_canonical_date(store, config, tmp_path, spelling):
    """Review F44: Python accepts `20260901` and `2026-W35-2` as 2026-09-01. Stored as
    given, every string comparison against the pin was wrong (a split reaching into the
    holdout passed) and the pin could not be corrected. Such a pin is refused, and one an
    older version stored reads back canonical, so the guard holds."""
    registry, dataset_id, _ = registered(store, config, tmp_path, VALIDATION)
    try:
        assert date.fromisoformat(spelling) == date(2026, 9, 1)
        with pytest.raises(ValueError, match="canonical"):
            registry.pin_holdout(dataset_id, spelling, "operator")
        assert registry.holdout_from(dataset_id) is None
        registry.db.execute(  # a pin written before this check existed
            "INSERT INTO holdout_pins(dataset_id,holdout_from,actor,at) VALUES (?,?,?,?)",
            (dataset_id, spelling, "older", "2026-10-01T00:00:00+00:00"),
        )
        assert registry.holdout_from(dataset_id) == "2026-09-01"
        late = SplitDefinition("2026-07-31", "2026-09-15", embargo_days=1)
        with pytest.raises(ValueError, match="reaches the pinned holdout"):
            splits.guard(registry, dataset_id, late, pin_if_missing=True)
        assert registry.pin_holdout(dataset_id, "2026-09-01", "operator") == "2026-09-01"
    finally:
        registry.close()


def test_cli_backtests_read_the_registry_read_only_except_the_holdout(store, config, tmp_path):
    registry, dataset_id, model_id = registered(store, config, tmp_path, VALIDATION)
    registry.pin_holdout(dataset_id, VALIDATION.first_holdout_date, "test")
    registry.close()
    root = tmp_path / "research"

    def backtest_cli(segment, ledger, *extra):
        return execute(
            parser().parse_args(
                ["backtest", "run", "--registry", str(root), "--history-db", str(config.database)]
                + ["--dataset", dataset_id, "--model", model_id, "--segment", segment]
                + ["--development-through", VALIDATION.development_through]
                + ["--validation-through", VALIDATION.validation_through]
                + ["--ledger", str(tmp_path / ledger), *extra]
            )
        )

    # Another process holding the registry writer lock (e.g. a concurrent backtest
    # opening it) does not stop a validation backtest, which never writes.
    with open(root / "research.sqlite3.lockfile", "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = backtest_cli("validation", "v.sqlite3")
        assert result["status"] == "complete" and result["segment"] == "validation"
        with pytest.raises(RuntimeError, match="registry lock"):
            backtest_cli("holdout", "h.sqlite3", "--acknowledge-holdout")


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


def test_a_reused_holdout_is_selection_biased_and_archives_must_match(store, config, tmp_path):
    registry, dataset_id, model_id = registered(store, config, tmp_path, HOLDOUT)
    try:
        artifact = ModelArtifact.from_dict(json.loads(registry.model_artifact(model_id)))
        other = replace(artifact, training=artifact.training | {"variant": "second model"})
        second = registry.register_model(
            other.to_json(), family="logistic", dataset_id=dataset_id, summary={"test": True}
        )
        options = dict(segment="holdout", min_holdout_rows=1, acknowledge_holdout=True)
        first = backtest.run(
            registry, store.db, dataset_id, model_id, HOLDOUT, PaperConfig(),
            ledger_path=tmp_path / "a.sqlite3", **options,
        )  # fmt: skip
        reused = backtest.run(
            registry, store.db, dataset_id, second, HOLDOUT, PaperConfig(),
            ledger_path=tmp_path / "b.sqlite3", **options,
        )  # fmt: skip
        assert first["selection_biased"] is False and reused["selection_biased"] is True
        assert reused["holdout_ledger"]["evaluations"] == 2
        assert first["markets_with_execution_data"] <= first["markets"]
        manifest = registry.dataset(dataset_id)
        backtest.check_archive(store.db, manifest)
        forged = json.loads(json.dumps(manifest))
        forged["source"]["archive"]["latest_fetch_digest"] = "0" * 64
        with pytest.raises(ValueError, match="differs from the archive"):
            backtest.check_archive(store.db, forged)
    finally:
        registry.close()


def test_cli_refuses_several_depths_for_one_ledger_run(tmp_path):
    """N-26b (fixed in 9f06a3d): one --ledger run takes one --assumed-depth; nothing is
    silently ignored."""
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
            "--ledger",
            str(tmp_path / "l.sqlite3"),
            "--assumed-depth",
            "5",
            "--assumed-depth",
            "25",
        ]
    )
    with pytest.raises(ValueError, match="one --assumed-depth"):
        execute(args)
    assert not (tmp_path / "r").exists() and not (tmp_path / "l.sqlite3").exists()


def test_summary_takes_selection_bias_from_the_holdout_ledger():
    """N-26c: no default derived from the segment name, which called a reused holdout
    unbiased; the caller passes what the registry's holdout ledger recorded."""
    execution = {"snapshots": [], "markets": {}, "skipped": {}, "window_seconds": 60}
    envelope = dict(
        segment="holdout", model_id="m", dataset_id="d", rows=[], n_forecasts=0, execution=execution
    )
    with pytest.raises(TypeError, match="selection_biased"):
        backtest.summary({}, **envelope)
    assert backtest.summary({}, **envelope, selection_biased=True)["selection_biased"] is True


def test_library_holdout_runs_require_an_acknowledgement(store, config, tmp_path):
    """F34 (R64): `simulate` and `run` take the final holdout only with an explicit
    `acknowledge_holdout=True`, as the CLI requires --acknowledge-holdout. Without it
    nothing is consumed, no ledger slot is taken and no paper ledger is written."""
    registry, dataset_id, model_id = registered(store, config, tmp_path, HOLDOUT)
    try:
        key = splits.holdout_key(registry, dataset_id, HOLDOUT)
        rows = backtest.segment_rows(registry, dataset_id, HOLDOUT, "holdout")
        synthetic = backtest.execution_inputs(store.db, rows) | {
            "source": {"interpretation": "synthetic", "run_id": None}
        }
        ledger = tmp_path / "h.sqlite3"
        options = dict(segment="holdout", ledger_path=ledger, min_holdout_rows=1)
        with pytest.raises(ValueError, match="acknowledge_holdout"):
            backtest.simulate(
                registry, synthetic, dataset_id, model_id, HOLDOUT, PaperConfig(), **options
            )
        with pytest.raises(ValueError, match="acknowledge_holdout"):
            backtest.run(
                registry, store.db, dataset_id, model_id, HOLDOUT, PaperConfig(), **options
            )
        assert not ledger.exists()
        assert registry.holdout_status(dataset_id, key)["untouched"]
        assert registry.holdout_evaluation(model_id, dataset_id, key) is None
        result = backtest.run(
            registry, store.db, dataset_id, model_id, HOLDOUT, PaperConfig(),
            acknowledge_holdout=True, **options,
        )  # fmt: skip
        assert result["holdout_ledger"]["evaluations"] == 1
    finally:
        registry.close()
