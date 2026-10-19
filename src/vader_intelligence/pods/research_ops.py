"""Implementations of the research operations, run inside pod workers (roadmap G).

Every operation reads only staged, verified inputs: a dataset snapshot (development and
validation rows, `pods.snapshot`), a candidate's model artifact and, for paper backtests,
coordinator-built candle execution inputs (`research.backtest`). Outputs are artifacts
and a small summary. Nothing an operation returns is promotion evidence: pod metrics are
labelled diagnostics, and the protected evaluator (`pods.evaluator`) recomputes every
metric a promotion relies on from the research registry.

Heavy modules (numpy, scikit-learn, the simulator) are imported inside the operations so
that the coordinator, which imports the operation registry, stays light.
"""

import json
import tempfile
from decimal import Decimal

from .contracts import require
from .operations import (
    DEFAULT_SEARCH_SECONDS,
    FEATURE_DROP_PREFIX,
    DeterministicFailure,
    EvaluateCandidateParams,
    PaperBacktestParams,
    Partial,
    RobustnessAnalysisParams,
    SymbolicSearchParams,
    TrainModelParams,
    register_operation,
)
from .snapshot import EXECUTION_KIND, SNAPSHOT_KIND, SnapshotRefused, feature_set_version, load

MODEL_KIND = "model_artifact"
DIAGNOSTICS_KIND = "pod_diagnostics"
SEARCH_RECORD_KIND = "search_record"
ROBUSTNESS_KIND = "robustness_report"
PAPER_KIND = "paper_backtest_report"
NOT_EVIDENCE = (
    "pod diagnostic: not promotion evidence; the protected evaluator recomputes every "
    "metric from the registry"
)
FAMILIES = {"logistic": "logistic", "gradient_boosting": "gbm"}


def _snapshot(ctx, sha256):
    try:
        return load(ctx.read_input(sha256))
    except (SnapshotRefused, KeyError, TypeError, ValueError) as exc:
        raise DeterministicFailure(f"snapshot refused: {exc}") from None


def _emit_json(ctx, name, payload, kind):
    from ..provenance import json_text

    return ctx.emit(name, json_text(payload).encode(), kind)


def _candidate_model(store, candidate_id):
    row = store.db.execute(
        "SELECT kind, spec_json FROM candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    require(row is not None, f"unknown candidate {candidate_id}")
    require(row["kind"] == "model", "candidate is not a model")
    return json.loads(row["spec_json"])["model_sha256"]


def snapshot_input(params, store):
    return ((params.dataset_manifest_hash, SNAPSHOT_KIND),)


def candidate_inputs(params, store):
    model = _candidate_model(store, params.candidate_id)
    return ((params.dataset_manifest_hash, SNAPSHOT_KIND), (model, MODEL_KIND))


def backtest_inputs(params, store):
    return candidate_inputs(params, store) + ((params.execution_inputs_hash, EXECUTION_KIND),)


def _model(ctx, sha256, snapshot):
    from ..models.artifact import ModelArtifact

    try:
        artifact = ModelArtifact.from_dict(json.loads(ctx.read_input(sha256)))
    except (KeyError, TypeError, ValueError) as exc:
        raise DeterministicFailure(f"candidate model refused: {exc}") from None
    if artifact.training.get("feature_set") != snapshot.feature_set:
        raise DeterministicFailure("candidate model uses another feature set")
    return artifact


# Training --------------------------------------------------------------------------------


@register_operation(
    "train_model",
    TrainModelParams,
    roles=("baseline_models",),
    replace=True,
    inputs=snapshot_input,
)
def train_model(params: TrainModelParams, ctx):
    from ..research import training

    snapshot = _snapshot(ctx, params.dataset_manifest_hash)
    if params.feature_set != snapshot.feature_set:
        raise DeterministicFailure("feature_set differs from the snapshot")
    if params.feature_set_version != feature_set_version(snapshot.manifest):
        raise DeterministicFailure("feature_set_version differs from the snapshot")
    h = params.hyperparameters
    try:
        artifact, result, grid = training.fit(
            snapshot.manifest,
            snapshot.dataset_id,
            snapshot.development,
            family=FAMILIES[params.model_family],
            split=snapshot.split,
            grid=h.get("grid") or None,
            n_folds=h.get("n_folds", 4),
            seed=params.seed,
            max_configs=h.get("max_configs", 24),
            # Declared, not the remaining wall time: the budget is recorded in the
            # artifact, so a retried task must pass the same value to reproduce it.
            deadline_seconds=float(h.get("search_seconds", DEFAULT_SEARCH_SECONDS)),
            calibration=h.get("calibration", "platt"),
        )
    except ValueError as exc:
        raise DeterministicFailure(str(exc)) from None
    model = _emit_json(ctx, "model.json", json.loads(artifact.to_json()), MODEL_KIND)
    diagnostics = training.validation_metrics(
        artifact, snapshot.manifest, snapshot.validation, seed=params.seed
    )
    _emit_json(ctx, "diagnostics.json", {"note": NOT_EVIDENCE, **diagnostics}, DIAGNOSTICS_KIND)
    output = {
        "model_sha256": model.sha256,
        "family": artifact.family,
        "selected_config": result.selected_config,
        "search_stop_reason": result.stop_reason,
        "configs_tried": len(result.results),
        "grid": grid,
        "development_rows": len(snapshot.development),
        "diagnostic_validation_log_loss": diagnostics["model"]["log_loss"],
        "evidence": NOT_EVIDENCE,
    }
    return Partial(output) if result.stop_reason == "deadline" else output


# Symbolic search -------------------------------------------------------------------------


@register_operation(
    "symbolic_search",
    SymbolicSearchParams,
    roles=("symbolic_search",),
    replace=True,
    inputs=snapshot_input,
)
def symbolic_search_op(params: SymbolicSearchParams, ctx):
    from ..research import symbolic_search
    from ..symbolic import SearchConfig

    snapshot = _snapshot(ctx, params.dataset_manifest_hash)
    config = SearchConfig(
        population=params.population,
        generations=params.max_generations,
        max_candidates=params.max_candidates,
        max_seconds=params.max_seconds,  # declared (recorded in the search config)
        seed=params.seed,
    )
    try:
        fitted = symbolic_search.fit(
            snapshot.manifest,
            snapshot.dataset_id,
            snapshot.development,
            split=snapshot.split,
            config=config,
        )
    except ValueError as exc:
        raise DeterministicFailure(str(exc)) from None
    record = fitted["record"]
    _emit_json(
        ctx,
        "search_record.json",
        {"experiment_manifest": fitted["experiment_manifest"], "search": record},
        SEARCH_RECORD_KIND,
    )
    output = {
        "stop_reason": record["stop_reason"],
        "candidates_evaluated": record["counters"]["candidates_evaluated"],
        "generations_completed": record["generations_completed"],
        "model_sha256": None,
        "selection_biased": True,
        "evidence": NOT_EVIDENCE,
    }
    artifact = fitted["artifact"]
    if artifact is None:
        return output | {"status": "no_valid_expression"}
    model = _emit_json(ctx, "model.json", json.loads(artifact.to_json()), MODEL_KIND)
    diagnostics = symbolic_search.validation_metrics(
        artifact, snapshot.manifest, snapshot.validation, seed=params.seed
    )
    _emit_json(ctx, "diagnostics.json", {"note": NOT_EVIDENCE, **diagnostics}, DIAGNOSTICS_KIND)
    return output | {
        "status": "complete",
        "model_sha256": model.sha256,
        "expression": record["selected"]["description"],
        "complexity": record["selected"]["complexity"],
        "parity": fitted["parity"],
        "diagnostic_validation_log_loss": diagnostics["model"]["log_loss"],
    }


# Candidate diagnostics -------------------------------------------------------------------


def _candidate(ctx, params):
    snapshot = _snapshot(ctx, params.dataset_manifest_hash)
    models = ctx.staged_inputs().get(MODEL_KIND, [])
    if len(models) != 1:
        raise DeterministicFailure("exactly one candidate model must be staged")
    return snapshot, models[0], _model(ctx, models[0], snapshot)


@register_operation(
    "evaluate_candidate",
    EvaluateCandidateParams,
    roles=("baseline_models", "symbolic_search"),
    replace=True,
    inputs=candidate_inputs,
)
def evaluate_candidate(params: EvaluateCandidateParams, ctx):
    from ..research import training

    snapshot, model_sha, artifact = _candidate(ctx, params)
    rows = snapshot.development if params.split == "development" else snapshot.validation
    metrics = training.validation_metrics(artifact, snapshot.manifest, rows, seed=0)
    selected = {name: metrics["model"][name] for name in params.metrics}
    payload = {
        "note": NOT_EVIDENCE,
        "split": params.split,
        "in_sample": params.split == "development",
        "model_sha256": model_sha,
        "metrics": selected,
        "baselines": metrics["baselines"],
    }
    _emit_json(ctx, "diagnostics.json", payload, DIAGNOSTICS_KIND)
    return {"model_sha256": model_sha, "split": params.split, **selected, "evidence": NOT_EVIDENCE}


# Robustness ------------------------------------------------------------------------------


def _slice(rows, name, spread_median):
    if name == "regular_season_only":
        return [r for r in rows if not r["features"].get("postseason")]
    if name == "postseason_only":
        return [r for r in rows if r["features"].get("postseason")]
    if name in ("tight_spread", "wide_spread"):
        known = [r for r in rows if r["features"].get("spread_c") is not None]
        if name == "tight_spread":
            return [r for r in known if r["features"]["spread_c"] <= spread_median]
        return [r for r in known if r["features"]["spread_c"] > spread_median]
    raise ValueError(name)


@register_operation(
    "robustness_analysis",
    RobustnessAnalysisParams,
    roles=("robustness_execution",),
    replace=True,
    inputs=candidate_inputs,
)
def robustness_analysis(params: RobustnessAnalysisParams, ctx):
    import numpy as np

    from ..models.artifact import predict_proba
    from ..research import training

    snapshot, model_sha, artifact = _candidate(ctx, params)
    rows = snapshot.validation
    names = [f["name"] for f in snapshot.manifest["feature_set"]["features"]]
    spreads = [r["features"]["spread_c"] for r in rows if r["features"].get("spread_c") is not None]
    spread_median = float(np.median(spreads)) if spreads else 0.0

    def compare(subset, *, drop=None):
        if len(subset) < training.MIN_EVALUATION_ROWS:
            return {"n": len(subset), "status": "too_few_rows"}
        X, y, _, groups = training.matrices(subset, names)
        if drop is not None:
            X = X.copy()
            X[:, names.index(drop)] = np.nan
        result = training.compare(
            predict_proba(artifact, X), subset, names, y, groups, seed=params.seed
        )
        market = result["baselines"].get("market:mid_c", {})
        return {
            "n": len(subset),
            "model_log_loss": result["model"]["log_loss"],
            "market_coverage": market.get("coverage"),
            "model_minus_market_log_loss": market.get("model_minus_baseline_log_loss"),
        }

    slices = {}
    for name in params.perturbations:
        if name == "by_month":
            months = sorted({r["official_date"][:7] for r in rows})
            slices[name] = {
                m: compare([r for r in rows if r["official_date"][:7] == m]) for m in months
            }
        elif name.startswith(FEATURE_DROP_PREFIX):
            feature = name[len(FEATURE_DROP_PREFIX) :]
            if feature not in names:
                raise DeterministicFailure(f"feature {feature!r} is not in the feature set")
            slices[name] = compare(rows, drop=feature)
        else:
            slices[name] = compare(_slice(rows, name, spread_median))
    payload = {
        "note": NOT_EVIDENCE,
        "segment": "validation",
        "selection_biased": True,
        "model_sha256": model_sha,
        "spread_median": spread_median,
        "overall": compare(rows),
        "slices": slices,
    }
    _emit_json(ctx, "robustness.json", payload, ROBUSTNESS_KIND)
    return {"model_sha256": model_sha, "slices": sorted(slices), "evidence": NOT_EVIDENCE}


# Paper backtest --------------------------------------------------------------------------


@register_operation(
    "paper_backtest",
    PaperBacktestParams,
    roles=("robustness_execution",),
    replace=True,
    inputs=backtest_inputs,
)
def paper_backtest(params: PaperBacktestParams, ctx):
    from ..research import backtest
    from ..trading.fees import PROVISIONAL_SCHEDULES
    from ..trading.simulator import (
        LatencyModel,
        PaperConfig,
        StrategyConfig,
        sensitivity,
    )

    snapshot, model_sha, _ = _candidate(ctx, params)
    try:
        execution = backtest.load_execution(ctx.read_input(params.execution_inputs_hash))
    except ValueError as exc:
        raise DeterministicFailure(str(exc)) from None
    model_json = ctx.read_input(model_sha)
    forecasts = backtest.forecasts(model_json, snapshot.manifest, snapshot.validation, model_sha)
    inputs = backtest.paper_inputs(
        execution, forecasts, f"history-validation:{snapshot.dataset_id}"
    )
    if not inputs.forecasts:
        raise DeterministicFailure("no validation forecast has execution data")
    (fees,) = [f for f in PROVISIONAL_SCHEDULES if f.version == params.fee_schedule_version]
    config = PaperConfig(
        strategy=StrategyConfig(min_edge=Decimal(params.min_edge)),
        fee_schedule=fees,
    )
    slow = LatencyModel(submit_ms=(1250, 1250))
    grid = {
        "fee_schedules": (fees,),
        "extra_slippage_ticks": (0, 1),
        "latency": (config.latency, slow),
        "maker_fill_model": ("none",),
        "assumed_depth": (None, *(Decimal(d) for d in params.assumed_depths)),
    }
    with tempfile.TemporaryDirectory(dir=ctx.workdir) as tmp:
        report = sensitivity(config, inputs, grid, directory=tmp).to_dict()
    payload = backtest.summary(
        report,
        segment="validation",
        model_id=model_sha,
        dataset_id=snapshot.dataset_id,
        rows=snapshot.validation,
        n_forecasts=len(inputs.forecasts),
        execution=execution,
    ) | {"note": NOT_EVIDENCE}
    _emit_json(ctx, "paper_backtest.json", payload, PAPER_KIND)
    return {
        "model_sha256": model_sha,
        "scenarios": len(report["rows"]),
        "forecasts": len(inputs.forecasts),
        "evidence": NOT_EVIDENCE,
    }
