"""Candidate lifecycle (G2 §6) and the holdout ledger; store-level, no processes."""

import sqlite3

import pytest

from vader_intelligence.pods.candidates import (
    CANDIDATE_STATES,
    CandidateSpec,
    EvaluatorMetricRecord,
    ExitDecision,
    HoldoutAlreadyConsumed,
    HoldoutEvaluationRecord,
    OperatorApprovalRecord,
    PaperReportRecord,
    PromotionRefused,
    ShadowEvaluationRecord,
    candidate_record,
    holdout_status,
    promote,
    propose_candidate,
    record_holdout_evaluation,
)
from vader_intelligence.pods.contracts import ContractError
from vader_intelligence.pods.store import status


def metrics(candidate_id, **overrides):
    values = dict(
        candidate_id=candidate_id,
        evaluator_version="eval-v1",
        source="protected_evaluator",
        dataset_manifest_hash="0" * 64,
        splits=("development", "validation"),
        metric="log_loss",
        lower_is_better=True,
        candidate_value=0.61,
        baseline_id="market-mid",
        baseline_value=0.65,
        paired_n=400,
        min_n=200,
        stability_passed=True,
        search_budget={"candidates": 5000, "seconds": 600},
        candidate_history_hash="1" * 64,
    )
    values.update(overrides)
    return EvaluatorMetricRecord(**values)


def shadow(candidate_id, **overrides):
    values = dict(
        candidate_id=candidate_id,
        forecaster_id="forecaster-1",
        evaluation_id="evaluation-1",
        mode="forward_shadow",
        coverage=0.8,
        n_forecasts=120,
        period_start="2026-09-01",
        period_end="2026-10-01",
    )
    values.update(overrides)
    return ShadowEvaluationRecord(**values)


def paper(candidate_id, **overrides):
    values = dict(
        candidate_id=candidate_id,
        report_id="paper-1",
        execution_assumptions="conservative",
        risk_checks_bypassed=0,
        n_decisions=80,
        period_start="2026-10-01",
        period_end="2026-10-15",
        execution_source="history_archive",
        execution_inputs_sha256="3" * 64,
    )
    values.update(overrides)
    return PaperReportRecord(**values)


def approval(candidate_id):
    return OperatorApprovalRecord(
        candidate_id=candidate_id,
        operator="operator@example.invalid",
        approved_at="2026-10-16T12:00:00+00:00",
        statement="approved for paper execution only",
    )


def holdout(candidate_id, value=0.62):
    return HoldoutEvaluationRecord(
        candidate_id=candidate_id,
        evaluator_version="eval-v1",
        source="protected_evaluator",
        dataset_manifest_hash="2" * 64,
        metric="log_loss",
        value=value,
        n=150,
    )


@pytest.fixture
def candidate(pods):
    pods.pod()
    task_id = pods.submit("noop")
    pods.accept(task_id, {"expression": {"var": "mid_logit"}})
    spec = CandidateSpec("diag", "expression", {"expression": {"var": "mid_logit"}}, task_id)
    return propose_candidate(pods.store, spec)


def state(pods, candidate_id):
    return candidate_record(pods.store, candidate_id)["state"]


def test_candidates_require_an_accepted_source_result(pods):
    pods.pod()
    pending = pods.submit("noop")
    with pytest.raises(ContractError):
        propose_candidate(pods.store, CandidateSpec("diag", "expression", {}, pending))
    pods.accept(pending)
    first = propose_candidate(pods.store, CandidateSpec("diag", "expression", {}, pending))
    again = propose_candidate(pods.store, CandidateSpec("diag", "expression", {}, pending))
    assert first == again and state(pods, first) == "proposed"
    record = candidate_record(pods.store, first)
    assert record["source_result_hash"] == pods.store.result(pending)["result_hash"]
    assert (
        record["experiment_manifest_hash"] == pods.store.task(pending)["experiment_manifest_hash"]
    )


def test_lifecycle_refuses_skipped_states_and_missing_or_untyped_evidence(pods, candidate):
    good = metrics(candidate)
    for target, evidence in (
        ("shadow_tested", [shadow(candidate)]),
        ("paper_approved", [paper(candidate), approval(candidate)]),
        ("validated", []),
        ("validated", [{"kind": "evaluator_metrics", "paired_n": 400}]),
        ("validated", [shadow(candidate)]),
        ("validated", [good, approval(candidate)]),
        ("validated", [metrics("f" * 32)]),
        ("validated", [metrics(candidate, paired_n=199)]),
        ("validated", [metrics(candidate, candidate_value=0.66)]),
        ("validated", [metrics(candidate, stability_passed=False)]),
        ("validated", [metrics(candidate, search_budget={})]),
        ("live_eligible", [good]),
    ):
        with pytest.raises(PromotionRefused):
            promote(pods.store, candidate, target, evidence)
        assert state(pods, candidate) == "proposed"
    with pytest.raises(ContractError):
        metrics(candidate, splits=("holdout",))
    with pytest.raises(ContractError):
        metrics(candidate, source="pod")
    assert promote(pods.store, candidate, "validated", [good]) == "validated"
    with pytest.raises(PromotionRefused):
        promote(pods.store, candidate, "paper_approved", [paper(candidate), approval(candidate)])
    with pytest.raises(PromotionRefused):
        promote(pods.store, candidate, "shadow_tested", [shadow(candidate, n_forecasts=0)])
    assert promote(pods.store, candidate, "shadow_tested", [shadow(candidate)]) == "shadow_tested"
    for evidence in (
        [paper(candidate)],
        [approval(candidate)],
        [paper(candidate, risk_checks_bypassed=1), approval(candidate)],
        [paper(candidate, execution_assumptions="optimistic"), approval(candidate)],
        # F40: the record names archive-built execution inputs and no depth scenario.
        [paper(candidate, execution_source=None), approval(candidate)],
        [paper(candidate, execution_source="supplied"), approval(candidate)],
        [paper(candidate, execution_inputs_sha256=None), approval(candidate)],
        [paper(candidate, assumed_depth="10"), approval(candidate)],
    ):
        with pytest.raises(PromotionRefused):
            promote(pods.store, candidate, "paper_approved", evidence)
    with pytest.raises(ContractError):
        OperatorApprovalRecord(candidate, "operator", "2026-10-16T12:00:00", "go live", "live")
    evidence = [approval(candidate), paper(candidate)]
    assert promote(pods.store, candidate, "paper_approved", evidence) == "paper_approved"
    record = candidate_record(pods.store, candidate)
    assert [(t["from"], t["to"]) for t in record["transitions"]] == [
        (None, "proposed"),
        ("proposed", "validated"),
        ("validated", "shadow_tested"),
        ("shadow_tested", "paper_approved"),
    ]
    assert sorted(e["kind"] for e in record["evidence"]) == [
        "evaluator_metrics",
        "operator_approval",
        "paper_report",
        "shadow_evaluation",
    ]
    assert not any("live" in name for name in CANDIDATE_STATES)
    schema = pods.store.db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='candidates'"
    ).fetchone()[0]
    assert "live" not in schema and all(f"'{name}'" in schema for name in CANDIDATE_STATES)
    retire = ExitDecision(candidate, "retired", "superseded", "operator")
    assert promote(pods.store, candidate, "retired", [retire]) == "retired"
    with pytest.raises(PromotionRefused):
        promote(
            pods.store, candidate, "quarantined", [ExitDecision(candidate, "quarantined", "x", "y")]
        )


def test_schema_refuses_direct_state_changes(pods, candidate):
    with pytest.raises(sqlite3.DatabaseError):
        pods.store.db.execute("UPDATE candidates SET state='paper_approved'")
    with pytest.raises(sqlite3.DatabaseError):
        pods.store.db.execute("UPDATE candidates SET state='validated'")  # no event recorded
    with pytest.raises(sqlite3.DatabaseError):
        pods.store.db.execute("DELETE FROM candidate_events")
    assert state(pods, candidate) == "proposed"


def test_side_exits_need_a_matching_exit_decision(pods, candidate):
    with pytest.raises(PromotionRefused):
        promote(pods.store, candidate, "rejected", [ExitDecision(candidate, "retired", "x", "y")])
    with pytest.raises(PromotionRefused):
        promote(pods.store, candidate, "retired", [ExitDecision(candidate, "retired", "x", "y")])
    rejected = ExitDecision(candidate, "rejected", "fails stability", "evaluator")
    assert promote(pods.store, candidate, "rejected", [rejected]) == "rejected"
    with pytest.raises(PromotionRefused):
        promote(pods.store, candidate, "validated", [metrics(candidate)])


def test_holdout_is_consumed_at_most_once_and_permanently(pods, candidate):
    with pytest.raises(PromotionRefused):
        record_holdout_evaluation(pods.store, holdout(candidate))  # not yet validated
    assert holdout_status(pods.store, candidate) == {"candidate_id": candidate, "consumed": False}
    promote(pods.store, candidate, "validated", [metrics(candidate)])
    digest = record_holdout_evaluation(pods.store, holdout(candidate))
    consumed = holdout_status(pods.store, candidate)
    assert consumed["consumed"] is True and consumed["evaluation_hash"] == digest
    for record in (holdout(candidate), holdout(candidate, value=0.5)):
        with pytest.raises(HoldoutAlreadyConsumed):
            record_holdout_evaluation(pods.store, record)
    for statement in ("DELETE FROM holdout_ledger", "UPDATE holdout_ledger SET consumed_at=0"):
        with pytest.raises(sqlite3.DatabaseError):
            pods.store.db.execute(statement)
    assert holdout_status(pods.store, candidate) == consumed
    assert candidate_record(pods.store, candidate)["holdout"]["consumed"] is True
    assert status(pods.store)["holdouts_consumed"] == 1
    retire = ExitDecision(candidate, "retired", "done", "operator")
    promote(pods.store, candidate, "retired", [retire])
    assert holdout_status(pods.store, candidate)["consumed"] is True
