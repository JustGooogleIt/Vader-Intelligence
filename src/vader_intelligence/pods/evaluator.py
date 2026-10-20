"""Protected evaluator: the coordinator-side bridge between pods, the research registry
and the candidate lifecycle (roadmap G).

Trust boundary:
- inputs to pods are built here: `snapshot_dataset` stores the development and
  validation rows of a registry dataset (never embargo or holdout rows) and
  `execution_snapshot` the candle execution inputs of the validation rows;
- `accept_model` turns an accepted pod result into a candidate: the model artifact is
  parsed, checked against the snapshot the task named, and registered in the research
  registry (its model id is its content digest);
- `evaluate` recomputes the validation comparison from the registry's own rows, never
  from pod output, and promotes to `validated` only when the criteria pass; otherwise
  the candidate is rejected with the evaluator's reasons;
- `record_shadow`, `record_paper` and `evaluate_holdout` add the later evidence. The
  holdout is evaluated at most once per candidate and is recorded in both ledgers (the
  registry's per dataset/split ledger and the pod store's per candidate ledger).

Validation is selection-biased by construction (it compares many pod proposals); the
number of candidates proposed on the same snapshot is part of every evaluator record.
No state names or implies live eligibility.
"""

import json
from dataclasses import asdict, dataclass

import numpy as np

from ..datasets import splits
from ..datasets.splits import SplitDefinition
from ..models import metrics
from ..models.artifact import ModelArtifact, check_inputs, predict_proba
from ..provenance import json_text
from ..research import backtest, training
from .candidates import (
    EVALUATOR,
    HOLDOUT_STATES,
    CandidateSpec,
    EvaluatorMetricRecord,
    ExitDecision,
    HoldoutEvaluationRecord,
    OperatorApprovalRecord,
    PaperReportRecord,
    ShadowEvaluationRecord,
    candidate_record,
    promote,
    propose_candidate,
    record_holdout_evaluation,
)
from .contracts import content_hash, require
from .research_ops import MODEL_KIND
from .snapshot import EXECUTION_KIND, SNAPSHOT_KIND, build, load

EVALUATOR_VERSION = "pods-evaluator-v1"
BASELINES = ("market:mid_c", "market:pair_mid_c", "constant")
DEFAULT_MIN_N = 100
INPUT_KINDS = (SNAPSHOT_KIND, EXECUTION_KIND)


@dataclass(frozen=True)
class Criteria:
    """Promotion criteria for proposed → validated.

    The candidate must beat the baseline on the paired validation rows, the paired
    bootstrap CI of (candidate − baseline) must lie below zero when `ci_below_zero`, and
    the paired estimate must also be negative in each chronological half (stability).
    """

    baseline: str = "market:mid_c"
    metric: str = "log_loss"
    min_n: int = 100
    ci_below_zero: bool = True
    seed: int = 0
    allow_weaker: bool = False  # explicit opt-in to criteria weaker than D-008; labelled

    def __post_init__(self):
        require(self.metric in ("log_loss", "brier"), "metric must be log_loss or brier")
        require(self.baseline in BASELINES, f"baseline must be one of {BASELINES}")
        require(type(self.min_n) is int and self.min_n >= 1, "min_n must be a positive integer")
        require(type(self.ci_below_zero) is bool, "ci_below_zero must be a boolean")

    def weaker_than_default(self):
        """Names of the criteria weaker than the D-008 default (N ≥ 100, CI below zero,
        a market baseline)."""
        out = []
        if self.min_n < DEFAULT_MIN_N:
            out.append("min_n")
        if not self.ci_below_zero:
            out.append("ci_below_zero")
        if self.baseline == "constant":
            out.append("baseline")
        return out


def _snapshot_sha(store, registry, dataset_id, split, now=None):
    pin = splits.guard(registry, dataset_id, split, pin_if_missing=True)
    rows = registry.dataset_rows(dataset_id)
    data = build(registry.dataset(dataset_id), rows, dataset_id, split, pin)
    return store.put_input(data, SNAPSHOT_KIND, now)


def snapshot_dataset(store, registry, dataset_id, split: SplitDefinition, now=None) -> str:
    """Store the development/validation snapshot a pod may read; returns its sha256."""
    return _snapshot_sha(store, registry, dataset_id, split, now)


def execution_snapshot(store, registry, history_db, dataset_id, split, now=None) -> str:
    """Store candle execution inputs for the validation rows; returns their sha256."""
    splits.guard(registry, dataset_id, split, pin_if_missing=True)
    backtest.check_archive(history_db, registry.dataset(dataset_id))
    _, validation = splits.development_and_validation(registry.dataset_rows(dataset_id), split)
    payload = backtest.execution_inputs(history_db, validation)
    return store.put_input(json_text(payload).encode(), EXECUTION_KIND, now)


def coordinator_input(store, sha, kind):
    """Require `sha` to be an input the coordinator stored itself (never a pod output)."""
    row = store.db.execute(
        "SELECT kind, source_task_id FROM artifacts WHERE sha256=?", (sha,)
    ).fetchone()
    require(row is not None and row[0] == kind, f"{sha} is not a stored {kind}")
    require(row[1] is None, f"{sha} was produced by a task, not stored by the coordinator")


def _task_snapshot(store, task):
    params = json.loads(task["params_json"])
    sha = params.get("dataset_manifest_hash")
    require(sha is not None, "task names no dataset snapshot")
    coordinator_input(store, sha, SNAPSHOT_KIND)
    return sha, load(store.artifacts.get_bytes(sha))


def accept_model(store, registry, task_id, now=None) -> str:
    """Register the model artifact of an accepted pod result and propose it."""
    task = store.task(task_id)
    require(task is not None, f"unknown task {task_id}")
    result = store.result(task_id)
    require(result is not None, "task has no accepted result")
    models = [a for a in result["result"]["artifacts"] if a["kind"] == MODEL_KIND]
    require(len(models) == 1, "result must carry exactly one model artifact")
    sha = models[0]["sha256"]
    data = store.artifacts.get_bytes(sha)
    artifact = ModelArtifact.from_dict(json.loads(data))
    snapshot_sha, snapshot = _task_snapshot(store, task)
    require(artifact.training.get("dataset_id") == snapshot.dataset_id, "dataset mismatch")
    require(artifact.training.get("split", {}).get("key") == snapshot.split.key, "split mismatch")
    require(artifact.training.get("feature_set") == snapshot.feature_set, "feature set mismatch")
    require(
        registry.holdout_from(snapshot.dataset_id) == snapshot.holdout_from,
        "snapshot holdout differs from the dataset's pinned holdout",
    )
    model_id = registry.register_model(
        data,
        family=artifact.family,
        dataset_id=snapshot.dataset_id,
        summary={"source": "research_pod", "pod_task_id": task_id, "pod_id": task["pod_id"]},
    )
    require(model_id == sha, "registry model id differs from the artifact digest")
    spec = {
        "model_sha256": sha,
        "family": artifact.family,
        "dataset_id": snapshot.dataset_id,
        "split_key": snapshot.split.key,
        "split": asdict(snapshot.split),
        "snapshot_sha256": snapshot_sha,
    }
    if task["state"] == "partial":  # stopped by its declared budget: flagged, not hidden
        spec["partial"] = True
    return propose_candidate(store, CandidateSpec(task["pod_id"], "model", spec, task_id), now)


def _paired(p, base, y, groups, metric, seed):
    estimate, low, high = metrics.paired_bootstrap(metric, p, base, y, groups, n=1000, seed=seed)
    return float(estimate), float(low), float(high)


def _search_budget(store, task_id):
    task = store.task(task_id)
    output = store.result(task_id)["result"]["output"]
    keys = ("configs_tried", "search_stop_reason", "grid", "candidates_evaluated", "stop_reason")
    return {
        "operation": task["type"],
        "params": json.loads(task["params_json"]),
        **{k: output[k] for k in keys if k in output},
    }


def _history_hash(store, snapshot_sha):
    rows = store.db.execute(
        "SELECT candidate_id FROM candidates WHERE json_extract(spec_json, '$.snapshot_sha256')=?"
        " ORDER BY candidate_id",
        (snapshot_sha,),
    ).fetchall()
    ids = [r[0] for r in rows]
    return content_hash({"snapshot": snapshot_sha, "candidates": ids}), len(ids)


def recompute(registry, spec, criteria: Criteria) -> dict:
    """The validation comparison from the registry's rows and the registered artifact."""
    split = SplitDefinition(**spec["split"])
    manifest = registry.dataset(spec["dataset_id"])
    _, validation = splits.development_and_validation(
        registry.dataset_rows(spec["dataset_id"]), split
    )
    artifact = ModelArtifact.from_dict(json.loads(registry.model_artifact(spec["model_sha256"])))
    names = [f["name"] for f in manifest["feature_set"]["features"]]
    check_inputs(artifact, names)  # the same columns, by name and order
    X, y, dates, groups = training.matrices(validation, names)
    p = predict_proba(artifact, X)
    feature = None if criteria.baseline == "constant" else criteria.baseline.split(":", 1)[1]
    if feature is None:
        base = np.full(len(y), 0.5)
    elif feature in names:
        base = np.array(
            [
                np.nan if r["features"][feature] is None else r["features"][feature]
                for r in validation
            ],
            dtype=np.float64,
        )
        base = np.clip(base, 1e-6, 1 - 1e-6)
    else:
        base = np.full(len(y), np.nan)
    covered = ~np.isnan(base)
    out = {"validation_rows": len(validation), "paired_n": int(covered.sum())}
    if covered.sum() < max(criteria.min_n, training.MIN_EVALUATION_ROWS):
        return out | {"status": "too_few_paired_rows"}
    pc, bc, yc, gc, dc = p[covered], base[covered], y[covered], groups[covered], dates[covered]
    metric_fn = metrics.log_loss if criteria.metric == "log_loss" else metrics.brier
    candidate_value = metric_fn(pc, yc)
    baseline_value = metric_fn(bc, yc)
    if criteria.metric == "log_loss":
        candidate_value, baseline_value = candidate_value[0], baseline_value[0]
    estimate, low, high = _paired(pc, bc, yc, gc, criteria.metric, criteria.seed)
    middle = np.sort(dc)[len(dc) // 2]
    halves = []
    for mask in (dc < middle, dc >= middle):
        if mask.sum() >= training.MIN_EVALUATION_ROWS:
            halves.append(
                _paired(pc[mask], bc[mask], yc[mask], gc[mask], criteria.metric, criteria.seed)[0]
            )
        else:
            halves.append(None)
    stable = all(h is not None and h < 0 for h in halves) and (
        high < 0 if criteria.ci_below_zero else estimate < 0
    )
    return out | {
        "status": "computed",
        "candidate_value": float(candidate_value),
        "baseline_value": float(baseline_value),
        "paired_delta": {"estimate": estimate, "ci95": [low, high]},
        "half_deltas": halves,
        "half_split_date": str(middle),
        "stability_passed": bool(stable),
    }


def evaluate(store, registry, candidate_id, criteria: Criteria | None = None, now=None) -> dict:
    """Recompute, record and decide: validated when every criterion passes, else rejected."""
    criteria = criteria or Criteria()
    weaker = criteria.weaker_than_default()
    require(
        not weaker or criteria.allow_weaker,
        f"criteria weaker than the D-008 default ({', '.join(weaker)}); pass allow_weaker "
        "to record a labelled decision",
    )
    record = candidate_record(store, candidate_id)
    require(record["state"] == "proposed", f"candidate is {record['state']}, not proposed")
    spec = record["spec"]
    computed = recompute(registry, spec, criteria)
    history_hash, proposals = _history_hash(store, spec["snapshot_sha256"])
    tasks = _tasks_on_snapshot(store, spec["snapshot_sha256"])
    labels = {"criteria_weaker_than_default": weaker, "model_tasks_on_snapshot": tasks}
    registry.record_evaluation(
        spec["model_sha256"],
        spec["dataset_id"],
        "validation:" + spec["split_key"],
        computed | labels | {"evaluator": EVALUATOR_VERSION, "criteria": asdict(criteria)},
        selection_biased=True,
    )
    decision = {"candidate_id": candidate_id, "recomputed": computed, "proposals": proposals}
    decision |= labels
    if computed["status"] != "computed":
        reason = f"evaluator: {computed['status']} ({computed['paired_n']} paired rows)"
        exit_ = ExitDecision(candidate_id, "rejected", reason, EVALUATOR)
        state = promote(store, candidate_id, "rejected", [exit_], now, expected="proposed")
        return decision | {"state": state}
    evidence = EvaluatorMetricRecord(
        candidate_id=candidate_id,
        evaluator_version=EVALUATOR_VERSION,
        source=EVALUATOR,
        dataset_manifest_hash=spec["snapshot_sha256"],
        splits=("validation",),
        metric=criteria.metric,
        lower_is_better=True,
        candidate_value=computed["candidate_value"],
        baseline_value=computed["baseline_value"],
        baseline_id=criteria.baseline,
        paired_n=computed["paired_n"],
        min_n=criteria.min_n,
        stability_passed=computed["stability_passed"],
        search_budget=_search_budget(store, record["source_task_id"])
        | {"proposals": proposals}
        | labels,
        candidate_history_hash=history_hash,
    )
    try:
        evidence.check()
    except ValueError as exc:
        exit_ = ExitDecision(candidate_id, "rejected", f"evaluator: {exc}"[:256], EVALUATOR)
        state = promote(store, candidate_id, "rejected", [exit_], now, expected="proposed")
        return decision | {"state": state}
    state = promote(store, candidate_id, "validated", [evidence], now, expected="proposed")
    return decision | {"state": state}


def _tasks_on_snapshot(store, snapshot_sha):
    """Model-producing tasks run on the snapshot by any pod (selection, beyond proposals)."""
    return store.db.execute(
        "SELECT COUNT(*) FROM tasks WHERE type IN ('train_model','symbolic_search') "
        "AND json_extract(params_json, '$.dataset_manifest_hash')=?",
        (snapshot_sha,),
    ).fetchone()[0]


def shadow_evidence(candidate_id, model_sha256, evaluation) -> ShadowEvaluationRecord:
    """Shadow evidence from a stored forecast evaluation (`inspect_evaluation` output)."""
    payload = evaluation["evaluation"]
    report = payload["report"]
    named = [
        (name, entry)
        for name, entry in report.get("forecasters", {}).items()
        if entry["artifact_sha256"] == model_sha256
    ]
    require(len(named) == 1, "the evaluation does not score this candidate exactly once")
    name, entry = named[0]
    universe = report["population"]["U"]
    period = report["period"]
    start = period["start_date"] or (period["earliest_cutoff"] or "")[:10]
    end = period["through_date"] or (period["latest_cutoff"] or "")[:10]
    require(bool(start and end), "evaluation period unknown")
    return ShadowEvaluationRecord(
        candidate_id=candidate_id,
        forecaster_id=name,
        evaluation_id=str(evaluation["evaluation_id"]),
        mode="forward_shadow" if payload["view"] == "forward-shadow" else "reconstruction",
        coverage=0.0 if not universe else min(1.0, entry["n"] / universe),
        n_forecasts=entry["n"],
        period_start=start,
        period_end=end,
    )


def record_shadow(store, candidate_id, evaluation, now=None) -> str:
    record = candidate_record(store, candidate_id)
    evidence = shadow_evidence(candidate_id, record["spec"]["model_sha256"], evaluation)
    return promote(store, candidate_id, "shadow_tested", [evidence], now)


def paper_evidence(store, candidate_id, summary) -> PaperReportRecord:
    """Paper evidence from a coordinator-run historical backtest (`research.backtest.run`)
    of this candidate's own model, dataset and split on its validation segment.

    The simulator has no path around the risk engine, so `risk_checks_bypassed` is zero by
    construction; decisions are the forecasts the strategy processed.
    """
    spec = candidate_record(store, candidate_id)["spec"]
    require(summary.get("kind") == "historical-paper-backtest", "not a backtest summary")
    require(summary.get("simulated") is True, "not a simulated backtest summary")
    require(summary.get("model_id") == spec["model_sha256"], "backtest of another model")
    require(summary.get("dataset_id") == spec["dataset_id"], "backtest of another dataset")
    require(summary.get("segment") == "validation", "paper evidence uses the validation segment")
    require("note" not in summary, "a pod's own backtest is never paper evidence")
    config = summary["report"]["manifest"]["config"]
    require(config["maker_fill_model"] == "none", "paper evidence requires maker model none")
    require(config["book_state_rule"] == "worst_of_bracketing", "paper evidence: worst_of")
    return PaperReportRecord(
        candidate_id=candidate_id,
        report_id=content_hash(summary),
        execution_assumptions="conservative",
        risk_checks_bypassed=0,
        n_decisions=summary["forecasts"],
        period_start=summary["period"]["start"],
        period_end=summary["period"]["end"],
    )


def record_paper(store, candidate_id, paper: PaperReportRecord, approval, now=None) -> str:
    require(isinstance(approval, OperatorApprovalRecord), "an operator approval is required")
    return promote(store, candidate_id, "paper_approved", [paper, approval], now)


def evaluate_holdout(store, registry, candidate_id, now=None) -> dict:
    """The candidate's one final-holdout evaluation, recorded in both ledgers."""
    record = candidate_record(store, candidate_id)
    require(not record["holdout"]["consumed"], "holdout already evaluated for this candidate")
    require(record["state"] in HOLDOUT_STATES, f"holdout only in {list(HOLDOUT_STATES)}")
    validations = [e["payload"] for e in record["evidence"] if e["kind"] == "evaluator_metrics"]
    require(
        all(not v["search_budget"].get("criteria_weaker_than_default") for v in validations),
        "the holdout requires a validation under the D-008 default criteria or stronger",
    )
    spec = record["spec"]
    split = SplitDefinition(**spec["split"])
    key = splits.holdout_key(registry, spec["dataset_id"], split)
    prior = registry.holdout_evaluation(spec["model_sha256"], spec["dataset_id"], key)
    if prior is None:
        result = training.evaluate_holdout(
            registry, spec["dataset_id"], spec["model_sha256"], split
        )
        holdout, recovered = result["holdout"], False
        kind = registry.holdout_evaluation(spec["model_sha256"], spec["dataset_id"], key)["kind"]
    else:  # a crash after the registry entry: record the same evaluation here, never twice
        holdout, recovered, kind = prior["metrics"], True, prior["kind"]
        result = {"status": "complete", "holdout": holdout}
    entry = HoldoutEvaluationRecord(
        candidate_id=candidate_id,
        evaluator_version=EVALUATOR_VERSION,
        source=EVALUATOR,
        dataset_manifest_hash=spec["snapshot_sha256"],
        metric="log_loss",
        value=float(holdout["model"]["log_loss"]),
        n=int(holdout["model"]["n"]),
        holdout_use=kind,
    )
    evaluation_hash = record_holdout_evaluation(store, entry, now)
    status = registry.holdout_status(spec["dataset_id"], key)
    return result | {
        "evaluation_hash": evaluation_hash,
        "registry_ledger": status,
        "holdout_use": kind,
        "recovered": recovered,
    }
