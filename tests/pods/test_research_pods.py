"""Research pods end to end on a SYNTHETIC registry dataset (roadmap G).

Real worker subprocesses train and search on a development/validation snapshot; the
protected evaluator registers their artifacts, recomputes the validation comparison from
the registry and promotes or rejects. The planted relation (outcome follows a sharpened
market mid) only proves the wiring; it is not evidence about markets.
"""

import hashlib
import json
from pathlib import Path

import pytest
from datasets.test_training import SPLIT, synthetic_dataset

from vader_intelligence.pods import evaluator
from vader_intelligence.pods.candidates import (
    OperatorApprovalRecord,
    PromotionRefused,
    candidate_record,
    holdout_status,
)
from vader_intelligence.pods.contracts import ContractError, ExperimentManifest, TaskSpec
from vader_intelligence.pods.snapshot import (
    EXECUTION_KIND,
    SnapshotRefused,
    feature_set_version,
    load,
)
from vader_intelligence.provenance import json_text
from vader_intelligence.research.registry import Registry

from ._harness import CODE_HASH


def tickered(rows):
    """Synthetic rows with the market identity a backtest needs."""
    out = []
    for row in rows:
        ticker = f"SYN-{row['group']}-A"
        out.append(
            row | {"ticker": ticker, "event_ticker": f"SYN-{row['group']}", "selected_team_id": 1}
        )
    return out


@pytest.fixture
def research(tmp_path):
    registry = Registry(tmp_path / "research")
    manifest, rows = synthetic_dataset()
    rows = tickered(rows)
    data = "".join(json_text(row) + "\n" for row in rows).encode()
    manifest = manifest | {"rows_sha256": hashlib.sha256(data).hexdigest()}
    dataset_id = registry.register_dataset(manifest, rows)
    try:
        yield registry, dataset_id, manifest, rows
    finally:
        registry.close()


def experiment(pods, snapshot_sha, operation, params, seed=0):
    manifest = ExperimentManifest(
        dataset_manifest_hash=snapshot_sha,
        feature_set_versions={"fs-market-v1": "synthetic"},
        split={
            "development": {"through": SPLIT.development_through},
            "validation": {"through": SPLIT.validation_through},
        },
        code_hash=CODE_HASH,
        operation=operation,
        params=params,
        seed=seed,
        budget={"max_seconds": 120},
    )
    return pods.store.put_experiment_manifest(manifest)


def submit(pods, pod, operation, params):
    snapshot_sha = params["dataset_manifest_hash"]
    manifest_sha = experiment(pods, snapshot_sha, operation, params)
    return pods.store.submit(TaskSpec(pod, operation, params, manifest_sha))


def train_params(snapshot_sha, manifest, **overrides):
    return {
        "dataset_manifest_hash": snapshot_sha,
        "feature_set": "fs-market-v1",
        "feature_set_version": feature_set_version(manifest),
        "model_family": "logistic",
        "seed": 0,
        "hyperparameters": {"grid": {"C": [0.1, 1.0]}, "n_folds": 3},
        **overrides,
    }


def test_snapshot_holds_only_development_and_validation_rows(research, pods):
    registry, dataset_id, manifest, rows = research
    sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    snapshot = load(pods.store.artifacts.get_bytes(sha))
    segments = [SPLIT.segment(r["official_date"]) for r in rows]
    assert len(snapshot.development) == segments.count("development")
    assert len(snapshot.validation) == segments.count("validation")
    assert segments.count("holdout") > 0  # the dataset has a holdout; the snapshot does not
    assert evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT) == sha
    # A snapshot carrying a holdout row in a declared segment is refused.
    payload = json.loads(pods.store.artifacts.get_bytes(sha))
    holdout_row = next(r for r, s in zip(rows, segments, strict=True) if s == "holdout")
    payload["segments"]["validation"].append(holdout_row)
    with pytest.raises(SnapshotRefused):
        load(json_text(payload).encode())
    payload["segments"]["holdout"] = []
    with pytest.raises(SnapshotRefused):
        load(json_text(payload).encode())


def test_pods_train_and_search_then_the_evaluator_decides(research, pods):
    registry, dataset_id, manifest, rows = research
    snapshot_sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    pods.pod("baseline", "baseline_models")
    pods.pod("symbolic", "symbolic_search")
    trained = submit(pods, "baseline", "train_model", train_params(snapshot_sha, manifest))
    searched = submit(
        pods,
        "symbolic",
        "symbolic_search",
        {
            "dataset_manifest_hash": snapshot_sha,
            "seed": 1,
            "max_candidates": 2000,
            "max_generations": 6,
            "max_seconds": 60.0,
            "population": 50,
        },
    )
    with pytest.raises(ContractError):  # a role may only run its own operations
        submit(pods, "symbolic", "train_model", train_params(snapshot_sha, manifest, seed=3))
    pods.coordinator().run(max_steps=20000, deadline_seconds=240)
    assert pods.state(trained) == "succeeded" and pods.state(searched) == "succeeded"

    # The worker saw only its staged inputs: the snapshot and the input listing.
    (attempt,) = pods.attempts(trained)
    staged = sorted(p.name for p in (Path(attempt["workdir"]) / "in").iterdir())
    assert staged == sorted([snapshot_sha, "inputs.json"])
    output = pods.store.result(trained)["result"]["output"]
    assert output["evidence"].startswith("pod diagnostic: not promotion evidence")

    # Accept: the artifact is registered under its digest and proposed.
    candidate = evaluator.accept_model(pods.store, registry, trained)
    record = candidate_record(pods.store, candidate)
    assert record["state"] == "proposed"
    assert registry.model_artifact(record["spec"]["model_sha256"])  # registered
    assert evaluator.accept_model(pods.store, registry, trained) == candidate  # idempotent
    symbolic = evaluator.accept_model(pods.store, registry, searched)

    # The evaluator recomputes from the registry. The planted sharpening beats raw mid on
    # the 120 validation rows, but the paired CI crosses zero: the default criteria reject.
    decided = evaluator.evaluate(pods.store, registry, candidate, evaluator.Criteria(min_n=50))
    recomputed = decided["recomputed"]
    assert recomputed["validation_rows"] == len(
        load(pods.store.artifacts.get_bytes(snapshot_sha)).validation
    )
    assert decided["proposals"] == 2  # both proposals on this snapshot are counted
    assert recomputed["candidate_value"] < recomputed["baseline_value"]
    assert recomputed["paired_delta"]["ci95"][1] > 0 and not recomputed["stability_passed"]
    assert decided["state"] == "rejected"
    (exit_,) = [
        e
        for e in candidate_record(pods.store, candidate)["evidence"]
        if e["kind"] == "exit_decision"
    ]
    assert exit_["payload"]["reason"] == "evaluator: stability checks failed"
    with pytest.raises(ContractError):  # decided once; never re-evaluated
        evaluator.evaluate(pods.store, registry, candidate)

    # A candidate that cannot meet min_n is rejected with the evaluator's reason.
    strict = evaluator.Criteria(min_n=10_000)
    rejected = evaluator.evaluate(pods.store, registry, symbolic, strict)
    assert rejected["state"] == "rejected"
    exits = [
        e
        for e in candidate_record(pods.store, symbolic)["evidence"]
        if e["kind"] == "exit_decision"
    ]
    assert "too_few_paired_rows" in exits[0]["payload"]["reason"]


def test_holdout_is_evaluated_once_and_recorded_in_both_ledgers(research, pods):
    registry, dataset_id, manifest, rows = research
    snapshot_sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    pods.pod("baseline", "baseline_models")
    trained = submit(pods, "baseline", "train_model", train_params(snapshot_sha, manifest))
    pods.coordinator().run(max_steps=20000, deadline_seconds=240)
    candidate = evaluator.accept_model(pods.store, registry, trained)
    with pytest.raises(ContractError):  # not validated yet: no holdout
        evaluator.evaluate_holdout(pods.store, registry, candidate)
    lenient = evaluator.Criteria(min_n=10, ci_below_zero=False)
    decided = evaluator.evaluate(pods.store, registry, candidate, lenient)
    assert decided["state"] == "validated"  # point estimate below mid in both halves
    result = evaluator.evaluate_holdout(pods.store, registry, candidate)
    assert result["registry_ledger"]["evaluations"] == 1
    assert holdout_status(pods.store, candidate)["consumed"] is True
    with pytest.raises(ContractError):
        evaluator.evaluate_holdout(pods.store, registry, candidate)


def synthetic_execution(rows):
    """Candle-like quotes one minute either side of each cutoff, settled by the label."""
    from datetime import datetime, timedelta

    markets, snapshots, settlements = [], [], []
    for row in rows:
        cutoff = datetime.fromisoformat(row["cutoff"])
        mid = row["features"]["mid_c"]
        bid, ask = round(max(0.01, mid - 0.01), 2), round(min(0.99, mid + 0.01), 2)
        markets.append(
            {
                "ticker": row["ticker"],
                "event_ticker": row["event_ticker"],
                "close_time": (cutoff + timedelta(hours=4)).isoformat(),
                "game_date": row["official_date"],
                "teams": [str(row["group"])],
            }
        )
        for offset in (-60, 60):
            snapshots.append(
                {
                    "ticker": row["ticker"],
                    "as_of": (cutoff + timedelta(seconds=offset)).isoformat(),
                    "yes_bid": f"{bid:.2f}",
                    "yes_ask": f"{ask:.2f}",
                    "ref": f"synthetic:{row['group']}:{offset}",
                }
            )
        settlements.append(
            {
                "ticker": row["ticker"],
                "result": "yes" if row["y"] else "no",
                "effective_at": (cutoff + timedelta(hours=5)).isoformat(),
                "ref": f"synthetic-label:{row['group']}",
            }
        )
    return {
        "schema": "vi-execution-inputs-v1",
        "source": {"interpretation": "synthetic", "run_id": None},
        "window_seconds": {"before": 60, "after": 60},
        "markets": markets,
        "snapshots": snapshots,
        "settlements": settlements,
        "skipped": {},
        "notice": "SYNTHETIC",
    }


def test_robustness_and_paper_backtest_pods_on_a_candidate(research, pods):
    registry, dataset_id, manifest, rows = research
    snapshot_sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    snapshot = load(pods.store.artifacts.get_bytes(snapshot_sha))
    execution_sha = pods.store.put_input(
        json_text(synthetic_execution(snapshot.validation)).encode(), EXECUTION_KIND
    )
    pods.pod("baseline", "baseline_models")
    pods.pod("robust", "robustness_execution")
    trained = submit(pods, "baseline", "train_model", train_params(snapshot_sha, manifest))
    coordinator = pods.coordinator()
    coordinator.run(max_steps=20000, deadline_seconds=240)
    candidate = evaluator.accept_model(pods.store, registry, trained)
    robust = submit(
        pods,
        "robust",
        "robustness_analysis",
        {
            "candidate_id": candidate,
            "dataset_manifest_hash": snapshot_sha,
            "perturbations": ["by_month", "tight_spread", "drop_feature:mid_c"],
            "seed": 0,
        },
    )
    paper = submit(
        pods,
        "robust",
        "paper_backtest",
        {
            "candidate_id": candidate,
            "dataset_manifest_hash": snapshot_sha,
            "fee_schedule_version": "kalshi-2026-02-general",
            "execution_inputs_hash": execution_sha,
            "assumed_depths": ["5"],
        },
    )
    coordinator.run(max_steps=40000, deadline_seconds=300)
    assert pods.state(robust) == "succeeded", pods.attempts(robust)
    assert pods.state(paper) == "succeeded", pods.attempts(paper)
    artifacts = {a["kind"]: a for a in pods.store.result(paper)["result"]["artifacts"]}
    report = json.loads(
        pods.store.artifacts.get_bytes(artifacts["paper_backtest_report"]["sha256"])
    )
    assert report["simulated"] is True and report["selection_biased"] is True
    rows_ = report["report"]["rows"]
    no_depth = [r for r in rows_ if r["scenario"]["assumed_depth"] is None]
    assumed = [r for r in rows_ if r["scenario"]["assumed_depth"] is not None]
    assert no_depth and all(r["fills"] == 0 for r in no_depth)  # candles supply no depth
    assert any(r["orders"] > 0 for r in rows_)
    assert assumed and report["notice"].startswith("Historical paper backtest")
    # Pod output is never evidence: a paper promotion needs the coordinator's own records.
    with pytest.raises((PromotionRefused, ContractError)):
        evaluator.record_paper(
            pods.store,
            candidate,
            evaluator.PaperReportRecord(
                candidate, "pod-report", "conservative", 0, 1, "2026-08-02", "2026-08-31"
            ),
            OperatorApprovalRecord(candidate, "op", "2026-10-19T00:00:00+00:00", "ok"),
        )


def test_cli_drives_a_pod_from_snapshot_to_decision(research, tmp_path):
    from vader_intelligence.cli import execute, parser

    registry, dataset_id, manifest, rows = research
    registry.close()  # the CLI opens its own handle
    root, reg = str(tmp_path / "orchestration"), str(tmp_path / "research")

    def run(*argv):
        return execute(parser().parse_args(["pods", *argv, "--root", root]))

    assert run("init")["status"] == "complete"
    run("register", "--pod", "baseline", "--role", "baseline_models")
    split = [
        "--development-through",
        SPLIT.development_through,
        "--validation-through",
        SPLIT.validation_through,
    ]
    snap = run("snapshot", "--registry", reg, "--dataset", dataset_id, *split)
    assert snap["validation_rows"] > 0 and snap["feature_set"] == "fs-market-v1"
    params = train_params(snap["snapshot_sha256"], manifest)
    assert params["feature_set_version"] == snap["feature_set_version"]
    task = run(
        "submit", "--pod", "baseline", "--operation", "train_model", "--params", json.dumps(params)
    )["task_id"]
    ran = run("run", "--deadline-seconds", "240")
    assert ran["run"]["stopped"] == "idle"
    candidate = run("accept", "--registry", reg, "--task", task)["candidate"]["candidate_id"]
    decided = run("evaluate", "--registry", reg, "--candidate", candidate, "--min-n", "50")
    assert decided["state"] == "rejected"  # see the stability note above
    shown = run("candidate", "--candidate", candidate)["candidate"]
    assert [t["to"] for t in shown["transitions"]] == ["proposed", "rejected"]
    with pytest.raises(ValueError, match="acknowledge-final"):
        run("holdout", "--registry", reg, "--candidate", candidate)


def test_a_retried_training_task_reproduces_the_clean_artifact(research, tmp_path):
    """SIGKILL the worker mid-task: the retry's artifact digest equals a clean run's (the
    search budget is a declared parameter, never the remaining wall time)."""
    import os
    import signal

    from ._harness import Harness, wait_for

    registry, dataset_id, manifest, rows = research
    digests = []
    for name, kill in (("clean", False), ("killed", True)):
        pods = Harness(tmp_path / name)
        try:
            snapshot = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
            pods.pod("baseline", "baseline_models")
            task = submit(pods, "baseline", "train_model", train_params(snapshot, manifest))
            coordinator = pods.coordinator()
            if kill:
                coordinator.step()
                os.kill(pods.claimed_pid(task), signal.SIGKILL)
            coordinator.run(max_steps=20000, deadline_seconds=240)
            wait_for(lambda: pods.state(task) == "succeeded", timeout=5)
            outcomes = [(a["outcome"], a["reason"]) for a in pods.attempts(task)]
            assert outcomes == ([("lost", "signal_9")] if kill else []) + [("succeeded", "result")]
            artifacts = pods.store.result(task)["result"]["artifacts"]
            digests.append(next(a["sha256"] for a in artifacts if a["kind"] == "model_artifact"))
        finally:
            pods.close()
    assert digests[0] == digests[1]
