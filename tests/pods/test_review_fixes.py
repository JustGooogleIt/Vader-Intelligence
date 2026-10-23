"""Protections added after the independent review of #26/#27, on SYNTHETIC data: the
pinned final holdout, coordinator-only inputs, candidate-bound diagnostics and evidence,
whitelisted baselines, decisions made on the current state, and budget-stopped searches
reported as partial."""

import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal

import pytest
from datasets.test_backtest import VALIDATION, registered
from datasets.test_training import SPLIT

from vader_intelligence.datasets.splits import SplitDefinition
from vader_intelligence.pods import evaluator
from vader_intelligence.pods.candidates import (
    CandidateSpec,
    ExitDecision,
    PaperReportRecord,
    PromotionRefused,
    candidate_record,
    promote,
    propose_candidate,
)
from vader_intelligence.pods.contracts import ContractError
from vader_intelligence.pods.snapshot import EXECUTION_KIND, SnapshotRefused, load
from vader_intelligence.provenance import json_text
from vader_intelligence.research import backtest
from vader_intelligence.trading.simulator import PaperConfig

from .test_pods_candidates import approval, metrics, shadow
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
    # Review F44: a non-canonical spelling of the pin would defeat the string checks.
    basic = date.fromisoformat(SPLIT.first_holdout_date).strftime("%Y%m%d")
    with pytest.raises(SnapshotRefused, match="canonical"):
        load(json_text(payload | {"holdout_from": basic}).encode())


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


def archive_candidate(pods, store, config, tmp_path):
    """A shadow_tested candidate whose model and dataset come from the SYNTHETIC history
    archive. Its earlier evidence is hand-built: only the paper evidence is under test."""
    registry, dataset_id, model_id = registered(store, config, tmp_path, VALIDATION)
    pods.pod()
    task = pods.submit("noop")
    pods.accept(task)
    spec = {
        "model_sha256": model_id,
        "family": "logistic",
        "dataset_id": dataset_id,
        "split_key": VALIDATION.key,
        "split": asdict(VALIDATION),
        "snapshot_sha256": "0" * 64,
    }
    candidate = propose_candidate(pods.store, CandidateSpec("diag", "model", spec, task))
    promote(pods.store, candidate, "validated", [metrics(candidate)])
    promote(pods.store, candidate, "shadow_tested", [shadow(candidate)])
    return registry, dataset_id, model_id, candidate


def test_supplied_execution_inputs_and_assumed_depth_are_never_paper_evidence(
    pods, store, config, tmp_path
):
    """F40 (R62, R62b): a backtest on supplied execution inputs (labelled synthetic
    quotes, or a payload claiming the archive interpretation) or under the assumed-depth
    SCENARIO is never conservative paper evidence, and settlements that contradict the
    dataset's labels are refused outright."""
    registry, dataset_id, model_id, candidate = archive_candidate(pods, store, config, tmp_path)
    args = (registry, dataset_id, model_id, VALIDATION)
    try:
        rows = backtest.segment_rows(registry, dataset_id, VALIDATION, "validation")
        built = backtest.execution_inputs(store.db, rows)
        synthetic = built | {"source": {"interpretation": "synthetic", "run_id": None}}
        # R62: labelled synthetic quotes with an assumed depth of 10 contracts.
        r62 = backtest.simulate(
            registry, synthetic, *args[1:], PaperConfig(assumed_depth=Decimal(10)),
            ledger_path=tmp_path / "r62.sqlite3",
        )  # fmt: skip
        with pytest.raises(ContractError):
            evaluator.paper_evidence(pods.store, candidate, r62)
        # R62b: every settlement flipped against the labels, claiming `history-v1`.
        flipped = built | {
            "settlements": [
                s | {"result": "no" if s["result"] == "yes" else "yes"}
                for s in built["settlements"]
            ]
        }
        with pytest.raises(ValueError, match="contradicts the dataset label"):
            backtest.simulate(
                *args[:1], flipped, *args[1:], PaperConfig(), ledger_path=tmp_path / "b.sqlite3"
            )
        stray = built | {"settlements": [*built["settlements"], built["settlements"][0]
                                         | {"ticker": "KXMLBGAME-OTHER"}]}  # fmt: skip
        with pytest.raises(ValueError, match="outside the segment rows"):
            backtest.simulate(
                *args[:1], stray, *args[1:], PaperConfig(), ledger_path=tmp_path / "s.sqlite3"
            )
        # Supplied inputs are labelled as supplied, whatever interpretation they claim.
        supplied = backtest.simulate(
            *args[:1], built, *args[1:], PaperConfig(), ledger_path=tmp_path / "c.sqlite3"
        )
        assert r62["execution_inputs"]["source"] == supplied["execution_inputs"]["source"]
        assert supplied["execution_inputs"]["source"] == "supplied"
        assert supplied["report"]["manifest"]["input_label"].startswith("supplied:history-v1:")
        assert r62["report"]["manifest"]["input_label"].startswith("supplied:synthetic:")
        with pytest.raises(ContractError, match="history archive"):
            evaluator.paper_evidence(pods.store, candidate, supplied)
        # An archive-built run under the assumed-depth scenario is refused as well.
        depth = backtest.run(
            registry, store.db, *args[1:], PaperConfig(assumed_depth=Decimal(10)),
            ledger_path=tmp_path / "d.sqlite3",
        )  # fmt: skip
        assert depth["execution_inputs"]["source"] == "history_archive"
        with pytest.raises(ContractError):
            evaluator.paper_evidence(pods.store, candidate, depth)
        with pytest.raises(ContractError, match="assumed depth"):
            evaluator.paper_backtest(
                pods.store, registry, store.db, candidate,
                PaperConfig(assumed_depth=Decimal(10)), tmp_path / "e.sqlite3",
            )  # fmt: skip
        assert not (tmp_path / "e.sqlite3").exists()
        assert candidate_record(pods.store, candidate)["state"] == "shadow_tested"
    finally:
        registry.close()


def test_paper_evidence_comes_only_from_the_coordinators_archive_backtest(
    pods, store, config, tmp_path
):
    """F40: paper evidence needs the summary of `evaluator.paper_backtest`, stored by the
    coordinator together with the execution inputs it built from the verified history
    archive; the record names that source, its digest and the (absent) depth scenario."""
    registry, dataset_id, model_id, candidate = archive_candidate(pods, store, config, tmp_path)
    try:
        # The same archive backtest run outside the evaluator was recorded by nobody.
        direct = backtest.run(
            registry, store.db, dataset_id, model_id, VALIDATION, PaperConfig(),
            ledger_path=tmp_path / "direct.sqlite3",
        )  # fmt: skip
        with pytest.raises(ContractError, match="not a stored paper_backtest_summary"):
            evaluator.paper_evidence(pods.store, candidate, direct)
        summary = evaluator.paper_backtest(
            pods.store, registry, store.db, candidate, PaperConfig(), tmp_path / "paper.sqlite3"
        )
        sha = summary["execution_inputs"]["sha256"]
        assert summary["execution_inputs"]["source"] == "history_archive"
        evaluator.coordinator_input(pods.store, sha, EXECUTION_KIND)
        paper = evaluator.paper_evidence(pods.store, candidate, summary)
        assert (paper.execution_source, paper.execution_inputs_sha256) == ("history_archive", sha)
        assert paper.assumed_depth is None and paper.n_decisions == summary["forecasts"]
        edited = json.loads(json_text(summary)) | {"forecasts": summary["forecasts"] + 1}
        with pytest.raises(ContractError, match="not a stored paper_backtest_summary"):
            evaluator.paper_evidence(pods.store, candidate, edited)
        forged = PaperReportRecord(
            candidate, "f" * 64, "conservative", 0, 5, "2026-09-20", "2026-09-20",
            execution_source="history_archive", execution_inputs_sha256=sha,
        )  # fmt: skip
        with pytest.raises(ContractError):
            evaluator.record_paper(pods.store, candidate, forged, approval(candidate))
        assert candidate_record(pods.store, candidate)["state"] == "shadow_tested"
        state = evaluator.record_paper(pods.store, candidate, paper, approval(candidate))
        assert state == "paper_approved"
    finally:
        registry.close()
