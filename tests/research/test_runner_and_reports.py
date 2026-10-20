"""Research runner (milestone 3) and reports/comparison (milestone 4).

Synthetic throughout. Builds on the F3.5 materialization helpers: one fixture game
forecast at C = 18:05Z, settled at 23:00Z the same day.
"""

import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forecast"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from _f34 import GAME_ID, at, collect, protocol, tick_at, to_schema4  # noqa: E402
from conftest import Clock  # noqa: E402
from test_materialization import (  # noqa: E402
    AS_OF,
    evaluate_at,
    forward_slice,
    settle,
    settlement_evidence,
)

from vader_intelligence.cli import execute, parser  # noqa: E402
from vader_intelligence.config import Config  # noqa: E402
from vader_intelligence.research import report as reports  # noqa: E402
from vader_intelligence.research.runner import inspect_run, list_runs, run  # noqa: E402
from vader_intelligence.storage import Store  # noqa: E402

PROTO = protocol()
GAME_DAY = date(2026, 9, 27)


def run_at(store, moment, **kwargs):
    clock = Clock()
    clock.origin = moment
    kwargs.setdefault("outcomes_as_of", AS_OF)
    kwargs.setdefault("start_date", GAME_DAY)
    kwargs.setdefault("through_date", GAME_DAY)
    return run(store, PROTO, clock=clock.now, monotonic=clock.mono, **kwargs)


def test_forward_run_evaluates_each_cohort_once_and_resumes_without_duplicates(
    store, config, monkeypatch
):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    first = run_at(store, at(23, 40), view="forward-shadow")
    assert first["status"] == "complete" and first["reconstruct"] == []
    (cohort,) = first["segments"]["whole"]["cohorts"].values()
    assert cohort["existing"] is False and cohort["population"]["scored_paired"] == 1
    assert cohort["mode"] == "synthetic"
    assert first["manifest"]["archive"]["forecast_decisions"] == 1
    assert first["manifest"]["protocol"] == PROTO.config_hash
    again = run_at(store, at(23, 50), view="forward-shadow")
    (cohort2,) = again["segments"]["whole"]["cohorts"].values()
    assert cohort2["existing"] is True and cohort2["evaluation_id"] == cohort["evaluation_id"]
    assert store.db.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 1
    assert (
        store.db.execute("SELECT COUNT(*) FROM runs WHERE kind='research-run'").fetchone()[0] == 2
    )
    inspected = inspect_run(store.db, first["run_id"])
    assert inspected["status"] == "complete" and inspected["summary"]["view"] == "forward-shadow"
    assert inspected["provenance"]["configuration"]["archive"]["fetch_ceiling"] >= 1
    assert list_runs(store.db)["count"] == 2


def test_reconstruction_run_reconstructs_then_evaluates_in_its_own_namespace(
    store, config, monkeypatch
):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    with pytest.raises(ValueError, match="own --dataset namespace"):
        run_at(store, at(23, 40), view="cutoff-reconstruction")
    result = run_at(store, at(23, 40), view="cutoff-reconstruction", dataset="study-1")
    assert result["status"] == "complete"
    (chunk,) = result["reconstruct"]
    assert chunk["decisions_eligible"] == 1 and chunk["dataset"] == "study-1"
    (cohort,) = result["segments"]["whole"]["cohorts"].values()
    assert cohort["population"]["scored_paired"] == 1
    assert Decimal(cohort["paired"]["mean_brier_midpoint"]) == Decimal("0.126025")
    # Forward and reconstruction results are separate evaluations of separate datasets.
    forward = run_at(store, at(23, 41), view="forward-shadow")
    ids = {
        forward["segments"]["whole"]["cohorts"][
            next(iter(forward["segments"]["whole"]["cohorts"]))
        ]["evaluation_id"],
        cohort["evaluation_id"],
    }
    assert len(ids) == 2
    datasets = {
        r[0]
        for r in store.db.execute(
            "SELECT json_extract(payload_json,'$.dataset') FROM evaluation_runs"
        )
    }
    assert datasets == {"study-1", PROTO.dataset}


def test_split_evaluates_development_before_holdout_and_records_history(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    with pytest.raises(ValueError, match="split boundaries"):
        run_at(
            store,
            at(23, 40),
            view="forward-shadow",
            start_date=GAME_DAY - timedelta(days=3),
            through_date=GAME_DAY,
            development_through=GAME_DAY,
            holdout_from=GAME_DAY - timedelta(days=1),
        )
    result = run_at(
        store,
        at(23, 40),
        view="forward-shadow",
        start_date=GAME_DAY - timedelta(days=3),
        through_date=GAME_DAY,
        development_through=GAME_DAY - timedelta(days=1),
        holdout_from=GAME_DAY,
    )
    assert list(result["segments"]) == ["development", "holdout"]
    dev = next(iter(result["segments"]["development"]["cohorts"].values()))
    hold = next(iter(result["segments"]["holdout"]["cohorts"].values()))
    assert dev["population"]["U"] == 0 and hold["population"]["U"] == 1
    assert dev["evaluation_id"] < hold["evaluation_id"]
    assert result["holdout_history"] == []
    # A second run over the same holdout range records the earlier evaluation and its
    # protocol hash, so a protocol change after seeing the holdout is visible.
    again = run_at(
        store,
        at(23, 50),
        view="forward-shadow",
        start_date=GAME_DAY - timedelta(days=3),
        through_date=GAME_DAY,
        development_through=GAME_DAY - timedelta(days=1),
        holdout_from=GAME_DAY,
    )
    assert again["holdout_history"][0]["evaluation_id"] == hold["evaluation_id"]
    assert again["holdout_history"][0]["research_config_hash"] == PROTO.config_hash


def test_holdout_history_lists_every_earlier_overlapping_evaluation(store, config, monkeypatch):
    """An evaluation that scored a holdout game is history whatever its range or view
    (review F12): a whole-range forward evaluation and an open-range simulated
    reconstruction evaluation both observed GAME_DAY."""
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    span = {"start_date": GAME_DAY - timedelta(days=3), "through_date": GAME_DAY}
    whole = run_at(store, at(23, 40), view="forward-shadow", **span)
    observed = next(iter(whole["segments"]["whole"]["cohorts"].values()))
    assert observed["population"]["scored_paired"] == 1  # the holdout game was scored
    other_view = evaluate_at(store, at(23, 41), view="cutoff-reconstruction")
    before = evaluate_at(
        store,
        at(23, 42),
        view="forward-shadow",
        start_date=GAME_DAY - timedelta(days=3),
        through_date=GAME_DAY - timedelta(days=1),
    )
    split = run_at(
        store,
        at(23, 50),
        view="forward-shadow",
        development_through=GAME_DAY - timedelta(days=1),
        holdout_from=GAME_DAY,
        **span,
    )
    history = {h["evaluation_id"]: h for h in split["holdout_history"]}
    assert list(history) == [observed["evaluation_id"], other_view["evaluation_id"]]
    assert before["evaluation_id"] not in history  # ends before the holdout range
    assert history[observed["evaluation_id"]]["view"] == "forward-shadow"
    assert history[other_view["evaluation_id"]]["view"] == "cutoff-reconstruction"
    assert history[other_view["evaluation_id"]]["start_date"] is None
    assert history[observed["evaluation_id"]]["research_config_hash"] == PROTO.config_hash


def test_budget_exhaustion_is_partial_and_writes_no_evaluation(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    ticks = iter([0.0, 1000.0])
    result = run(
        store,
        PROTO,
        view="forward-shadow",
        outcomes_as_of=AS_OF,
        start_date=GAME_DAY,
        through_date=GAME_DAY,
        clock=lambda: at(23, 40),
        monotonic=lambda: next(ticks, 1000.0),
        budget=10.0,
    )
    assert result["status"] == "partial" and result["errors"]
    assert store.db.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 0


def test_text_and_machine_reports(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    result = run_at(store, at(23, 40), view="forward-shadow")
    evaluation_id = next(iter(result["segments"]["whole"]["cohorts"].values()))["evaluation_id"]
    text = reports.text_report(store.db, evaluation_id)
    assert "view=forward-shadow" in text and "simulated view" in text
    assert "Population: U=1 E=1 K=1 M=1 P=1 | FK=1 FM=1 FP=1" in text
    assert "Paired scores (n=1): Brier constant 0.250000 midpoint 0.126025 delta -0.123975" in text
    assert "log loss constant 0.693147 midpoint 0.438505 delta -0.254642" in text
    assert "Not a trading result" in text
    document = reports.machine_report(store.db, evaluation_id, include_items=True)
    assert document["report"]["population"]["U"] == 1 and len(document["items"]) == 1
    assert document["population_manifest_digest"]
    json.dumps(document, allow_nan=False)  # serializable


def test_compare_restricts_to_common_scored_opportunities(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence(status="active"), moment=at(22, 0))
    pending = run_at(store, at(22, 30), view="forward-shadow", outcomes_as_of=at(22, 20))
    settle(store, config, monkeypatch, settlement_evidence())
    settled = run_at(store, at(23, 40), view="forward-shadow")
    first = next(iter(pending["segments"]["whole"]["cohorts"].values()))["evaluation_id"]
    second = next(iter(settled["segments"]["whole"]["cohorts"].values()))["evaluation_id"]
    comparison = reports.compare(store.db, first, second)
    assert comparison["same_population_manifest"] is True
    assert comparison["common_paired"]["n"] == 0
    assert comparison["coverage_difference"]["paired_scored_only_in_second"] == [f"game:{GAME_ID}"]
    text = reports.text_compare(comparison)
    assert "paired-only-in-second 1" in text and "Common paired opportunities n=0" in text
    # Same population scored in both: common n=1.
    settle(
        store,
        config,
        monkeypatch,
        settlement_evidence(result="no", payout="0.0000"),
        moment=at(23, 45),
    )
    corrected = run_at(store, at(23, 50), view="forward-shadow", outcomes_as_of=at(23, 46))
    third = next(iter(corrected["segments"]["whole"]["cohorts"].values()))["evaluation_id"]
    comparison = reports.compare(store.db, second, third)
    assert comparison["common_paired"]["n"] == 1
    assert comparison["per_opportunity"][f"game:{GAME_ID}"]["y_first"] == 1
    assert comparison["per_opportunity"][f"game:{GAME_ID}"]["y_second"] == 0
    # Different views are never compared.
    reconstruction = run_at(store, at(23, 55), view="cutoff-reconstruction", dataset="study-1")
    fourth = next(iter(reconstruction["segments"]["whole"]["cohorts"].values()))["evaluation_id"]
    with pytest.raises(ValueError, match="differ in view"):
        reports.compare(store.db, second, fourth)


def test_export_never_overwrites(tmp_path, store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    result = run_at(store, at(23, 40), view="forward-shadow")
    target = tmp_path / "run.json"
    assert reports.export(target, result) == str(target)
    assert json.loads(target.read_text())["run_id"] == result["run_id"]
    with pytest.raises(FileExistsError):
        reports.export(target, result)


def test_research_run_and_reports_through_the_cli(tmp_path, monkeypatch):
    path = tmp_path / "research.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)

    def cli(*args):
        return execute(parser().parse_args(["--db", str(path), *args]))

    cli("db-init")
    cli("settlement", "migrate")
    cli("forecast", "migrate")
    cli("forecast", "migrate", "--record-contract", "3")
    store = Store(path, min_free_bytes=1)
    try:
        to_schema4(store) if False else None
        collect(store, config, origin=at(18, 3))
        tick_at(store, PROTO, at(18, 3, 30))
        tick_at(store, PROTO, at(18, 5, 30))
        settle(store, config, monkeypatch, settlement_evidence())
    finally:
        store.close()
    research = tmp_path / "research.toml"
    research.write_text(f'[research]\ndataset = "{PROTO.dataset}"\n')
    out = tmp_path / "run.json"
    summary = cli(
        "research",
        "run",
        "--research-config",
        str(research),
        "--view",
        "cutoff-reconstruction",
        "--dataset",
        "study-cli",
        "--outcomes-as-of",
        AS_OF.isoformat(),
        "--from",
        "2026-09-27",
        "--through",
        "2026-09-27",
        "--out",
        str(out),
    )
    assert summary["status"] == "complete" and out.exists()
    evaluation_id = next(iter(summary["segments"]["whole"]["cohorts"].values()))["evaluation_id"]
    text = cli("evaluation", "report", "--id", str(evaluation_id))
    assert "view=cutoff-reconstruction" in text["text"]
    document = cli("evaluation", "report", "--id", str(evaluation_id), "--format", "json")
    assert document["report"]["population"]["scored_paired"] == 1
    listing = cli("research", "inspect")
    assert listing["count"] == 1
    detail = cli("research", "inspect", "--run-id", summary["run_id"])
    assert detail["summary"]["dataset"] == "study-cli"
