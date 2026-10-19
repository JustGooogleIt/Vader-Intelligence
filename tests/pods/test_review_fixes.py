"""Protections added after the independent review of #26/#27, on SYNTHETIC data: the
pinned final holdout, coordinator-only inputs, candidate-bound diagnostics and evidence,
whitelisted baselines, decisions made on the current state, and budget-stopped searches
reported as partial."""

import json

import pytest
from datasets.test_training import SPLIT

from vader_intelligence.datasets.splits import SplitDefinition
from vader_intelligence.pods import evaluator
from vader_intelligence.pods.candidates import ExitDecision, PromotionRefused, promote
from vader_intelligence.pods.contracts import ContractError
from vader_intelligence.pods.snapshot import SnapshotRefused, load
from vader_intelligence.provenance import json_text

from .test_research_pods import research, submit, train_params  # noqa: F401  (fixture)


def test_the_holdout_is_pinned_at_the_first_snapshot(research, pods):  # noqa: F811
    registry, dataset_id, manifest, rows = research
    sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    assert registry.holdout_from(dataset_id) == SPLIT.first_holdout_date
    snapshot = load(pods.store.artifacts.get_bytes(sha))
    assert snapshot.holdout_from == SPLIT.first_holdout_date
    late = SplitDefinition("2026-08-31", "2026-10-15", embargo_days=1)
    with pytest.raises(ValueError, match="reaches the pinned holdout"):
        evaluator.snapshot_dataset(pods.store, registry, dataset_id, late)
    # A forged snapshot that declares a late split and carries pinned-holdout rows, or
    # that drops the pin (v1), is refused by every reader.
    payload = json.loads(pods.store.artifacts.get_bytes(sha))
    held = [r for r in rows if r["official_date"] >= SPLIT.first_holdout_date]
    assert held
    forged = payload | {"split": late.to_dict()}
    forged["segments"] = {"development": payload["segments"]["development"], "validation": held}
    with pytest.raises(SnapshotRefused):
        load(json_text(forged).encode())
    moved = forged | {"holdout_from": "2026-12-31"}  # moving the pin does not help readers
    with pytest.raises(SnapshotRefused):
        load(json_text(moved | {"segments": payload["segments"]}).encode())
    legacy = {k: v for k, v in payload.items() if k != "holdout_from"}
    with pytest.raises(SnapshotRefused, match="rebuild"):
        load(json_text(legacy | {"schema": "vi-pod-dataset-v1"}).encode())


def test_pods_cannot_produce_inputs_and_diagnostics_stay_on_the_candidates_snapshot(
    research,  # noqa: F811
    pods,
):
    registry, dataset_id, manifest, rows = research
    snapshot_sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    pods.pod("diag", "diagnostics")
    forged = pods.submit(
        "emit_artifact", {"name": "snap.json", "text": "{}", "kind": "dataset_snapshot"}
    )
    pods.pod("baseline", "baseline_models")
    trained = submit(pods, "baseline", "train_model", train_params(snapshot_sha, manifest))
    pods.coordinator().run(max_steps=20000, deadline_seconds=240)
    assert pods.state(forged) == "quarantined"
    events = [e for e in pods.store.events(forged) if e["kind"] == "quarantined"]
    assert events[0]["detail"]["reason"] == "reserved_artifact_kind"
    assert (
        pods.store.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='dataset_snapshot' AND source_task_id IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    candidate = evaluator.accept_model(pods.store, registry, trained)
    # A second snapshot of the same dataset (earlier split): the candidate's diagnostics
    # may not run on it.
    other = evaluator.snapshot_dataset(
        pods.store, registry, dataset_id, SplitDefinition("2026-07-15", "2026-08-31", 1)
    )
    pods.pod("robust", "robustness_execution")
    params = {"candidate_id": candidate, "dataset_manifest_hash": other, "split": "validation"}
    with pytest.raises(ContractError, match="its own snapshot"):
        submit(pods, "robust", "evaluate_candidate", params)


def test_criteria_baselines_and_stale_decisions(research, pods):  # noqa: F811
    registry, dataset_id, manifest, rows = research
    for bad in ("market_mid", "market:last_price", "half"):
        with pytest.raises(ContractError, match="baseline"):
            evaluator.Criteria(baseline=bad)
    assert evaluator.Criteria().weaker_than_default() == []
    assert evaluator.Criteria(baseline="constant", allow_weaker=True).weaker_than_default() == [
        "baseline"
    ]
    snapshot_sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    pods.pod("baseline", "baseline_models")
    trained = submit(pods, "baseline", "train_model", train_params(snapshot_sha, manifest))
    pods.coordinator().run(max_steps=20000, deadline_seconds=240)
    candidate = evaluator.accept_model(pods.store, registry, trained)
    exit_ = ExitDecision(candidate, "rejected", "test", "protected_evaluator")
    with pytest.raises(PromotionRefused, match="not validated"):
        promote(pods.store, candidate, "rejected", [exit_], expected="validated")
    evaluator.evaluate(pods.store, registry, candidate)  # decided under the defaults
    model = json.loads(pods.store.db.execute(
        "SELECT spec_json FROM candidates WHERE candidate_id=?", (candidate,)
    ).fetchone()[0])["model_sha256"]  # fmt: skip
    summary = {
        "kind": "historical-paper-backtest",
        "simulated": True,
        "segment": "validation",
        "model_id": "f" * 64,
        "dataset_id": dataset_id,
    }
    with pytest.raises(ContractError, match="another model"):
        evaluator.paper_evidence(pods.store, candidate, summary)
    with pytest.raises(ContractError, match="validation segment"):
        evaluator.paper_evidence(
            pods.store, candidate, summary | {"model_id": model, "segment": "development"}
        )


def test_a_symbolic_search_stopped_by_wall_time_is_partial(research, pods):  # noqa: F811
    registry, dataset_id, manifest, rows = research
    snapshot_sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    pods.pod("symbolic", "symbolic_search")
    params = {
        "dataset_manifest_hash": snapshot_sha,
        "seed": 0,
        "max_candidates": 1_000_000,
        "max_generations": 10_000,
        "max_seconds": 0.05,
        "population": 50,
    }
    task = submit(pods, "symbolic", "symbolic_search", params)
    pods.coordinator().run(max_steps=20000, deadline_seconds=240)
    assert pods.state(task) == "partial"
    assert pods.store.result(task)["result"]["output"]["stop_reason"] == "max_seconds"
    candidate = evaluator.accept_model(pods.store, registry, task)
    spec = json.loads(pods.store.db.execute(
        "SELECT spec_json FROM candidates WHERE candidate_id=?", (candidate,)
    ).fetchone()[0])  # fmt: skip
    assert spec["partial"] is True


def test_train_model_grid_values_must_be_finite_numbers(research, pods):  # noqa: F811
    registry, dataset_id, manifest, rows = research
    snapshot_sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    pods.pod("baseline", "baseline_models")
    for grid in ({"C": [[1]]}, {"C": [None]}, {"C": [True]}, {"C": [float("inf")]}):
        params = train_params(snapshot_sha, manifest, hyperparameters={"grid": grid})
        with pytest.raises(ContractError):
            submit(pods, "baseline", "train_model", params)
    params = train_params(snapshot_sha, manifest, hyperparameters={"search_seconds": True})
    with pytest.raises(ContractError):
        submit(pods, "baseline", "train_model", params)
