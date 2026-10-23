"""Training, validation and one-time holdout evaluation of registered models (roadmap B3).

Lifecycle over a registry dataset and an explicit chronological split:
1. development rows only: grouped expanding-window CV grid search (preprocessing and
   model fit inside each fold on that fold's training rows), out-of-fold raw scores;
2. calibration fit on those out-of-fold scores (never on validation or holdout rows);
3. final preprocessing + model fit on all development rows with the selected
   configuration; a deterministic JSON artifact registered by content digest;
4. validation metrics against market and constant baselines on the same rows, recorded as
   selection-biased (validation is used to compare families and configurations);
5. the final holdout is read only by `evaluate_holdout`, which records the evaluation in
   the holdout ledger: the first consumes the holdout, every later one is a reuse.

Metrics are descriptive. A model that beats the market on a few hundred rows is not
evidence of edge, and no metric here is a trading result.
"""

import time

import numpy as np

from ..datasets import splits
from ..models import calibration as calibration_lib
from ..models import metrics, search
from ..models.artifact import ModelArtifact, dependency_versions, predict_proba
from ..models.preprocess import FeatureSpec
from ..provenance import source_hash

FAMILIES = ("logistic", "gbm")
DEFAULT_GRIDS = {
    "logistic": {"C": [0.01, 0.1, 1.0, 10.0]},
    "gbm": {
        "learning_rate": [0.05, 0.1],
        "max_iter": [100, 300],
        "max_leaf_nodes": [7, 15],
        "min_samples_leaf": [40],
        "l2_regularization": [1.0],
    },
}
CALIBRATIONS = ("platt", "isotonic", "none")
MARKET_BASELINES = ("mid_c", "pair_mid_c")
MIN_DEVELOPMENT_ROWS = 50
MIN_EVALUATION_ROWS = 10


def matrices(rows, names):
    """Feature matrix (NaN = missing), labels, official dates and groups, row order kept."""
    X = np.array(
        [
            [np.nan if r["features"][n] is None else float(r["features"][n]) for n in names]
            for r in rows
        ],
        dtype=np.float64,
    ).reshape(len(rows), len(names))
    y = np.array([r["y"] for r in rows], dtype=np.int64)
    dates = np.array([r["official_date"] for r in rows], dtype="datetime64[D]")
    groups = np.array([r["group"] for r in rows])
    return X, y, dates, groups


def feature_specs(manifest):
    return [
        FeatureSpec(f["name"], missing=f["missing"]) for f in manifest["feature_set"]["features"]
    ]


def _fit_calibration(kind, oof_raw, oof_y):
    if kind == "platt":
        return calibration_lib.fit_platt(oof_raw, oof_y)
    if kind == "isotonic":
        return calibration_lib.fit_isotonic(1 / (1 + np.exp(-oof_raw)), oof_y)
    return calibration_lib.identity()


def _r(value):
    return None if value is None else float(round(float(value), 8))


def score(p, y, groups, *, seed):
    """Descriptive metrics of one probability vector with grouped bootstrap intervals."""
    log_loss, clipped = metrics.log_loss(p, y)
    brier = metrics.brier(p, y)
    b_est, b_low, b_high = metrics.grouped_bootstrap("brier", p, y, groups, n=1000, seed=seed)
    l_est, l_low, l_high = metrics.grouped_bootstrap("log_loss", p, y, groups, n=1000, seed=seed)
    return {
        "n": int(len(y)),
        "positives": int(y.sum()),
        "brier": _r(brier),
        "brier_ci95": [_r(b_low), _r(b_high)],
        "log_loss": _r(log_loss),
        "log_loss_ci95": [_r(l_low), _r(l_high)],
        "log_loss_clipped": int(clipped),
        "ece": _r(metrics.ece(p, y)),
        "calibration_table": metrics.calibration_table(p, y, bins=10),
    }


def compare(model_p, rows, names, y, groups, *, seed):
    """Model metrics plus each baseline on its own coverage and the paired difference on
    the rows both cover (model − baseline; negative favours the model)."""
    out = {"model": score(model_p, y, groups, seed=seed), "baselines": {}}
    columns = {"constant": np.full(len(y), 0.5)}
    for feature in MARKET_BASELINES:
        if feature in names:
            values = np.array(
                [
                    np.nan if r["features"][feature] is None else r["features"][feature]
                    for r in rows
                ],
                dtype=np.float64,
            )
            columns["market:" + feature] = np.clip(values, 1e-6, 1 - 1e-6)
    for name, values in columns.items():
        covered = ~np.isnan(values)
        entry = {"coverage": int(covered.sum())}
        if covered.sum() >= MIN_EVALUATION_ROWS:
            entry["own"] = score(values[covered], y[covered], groups[covered], seed=seed)
            for metric in ("brier", "log_loss"):
                est, low, high = metrics.paired_bootstrap(
                    metric,
                    model_p[covered],
                    values[covered],
                    y[covered],
                    groups[covered],
                    n=1000,
                    seed=seed,
                )
                entry[f"model_minus_baseline_{metric}"] = {
                    "estimate": _r(est),
                    "ci95": [_r(low), _r(high)],
                }
        out["baselines"][name] = entry
    return out


def fit(
    manifest,
    dataset_id,
    development,
    *,
    family,
    split,
    grid=None,
    n_folds=4,
    seed=0,
    max_configs=24,
    deadline_seconds=900.0,
    calibration="platt",
    clock=time.monotonic,
):
    """Fit on development rows only and return (artifact, search result, grid).

    Needs no registry: research pods call it on a development/validation snapshot.
    """
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}")
    if calibration not in CALIBRATIONS:
        raise ValueError(f"calibration must be one of {CALIBRATIONS}")
    if len(development) < MIN_DEVELOPMENT_ROWS:
        raise ValueError(
            f"insufficient rows: development {len(development)} (≥{MIN_DEVELOPMENT_ROWS})"
        )
    names = [f["name"] for f in manifest["feature_set"]["features"]]
    specs = feature_specs(manifest)
    X, y, dates, groups = matrices(development, names)
    grid = grid or DEFAULT_GRIDS[family]
    result = search.grid_search(
        family,
        grid,
        X,
        y,
        dates,
        groups,
        specs,
        n_folds=n_folds,
        seed=seed,
        max_configs=max_configs,
        deadline_seconds=deadline_seconds,
        clock=clock,
        embargo_days=split.embargo_days,
    )
    calibrator = _fit_calibration(calibration, result.oof_raw, y[result.oof_index])
    preprocessor, params = search.fit_model(family, result.selected_config, X, y, specs, seed=seed)
    training = {
        "dataset_id": dataset_id,
        "dataset_name": manifest["name"],
        "evidence_kind": manifest["evidence_kind"],
        "feature_set": manifest["feature_set"]["name"],
        "forward_capable": manifest["feature_set"]["forward_capable"],
        "split": split.to_dict(),
        "development_rows": len(development),
        "family": family,
        "selected_config": result.selected_config,
        "search": result.summary(),
        "calibration": calibration,
        "calibration_fit_on": "out-of-fold development predictions",
        "seed": seed,
        "code_hash": source_hash(),
        "dependencies": dependency_versions(),
    }
    return ModelArtifact(family, preprocessor, params, calibrator, training), result, grid


def validation_metrics(artifact, manifest, validation, *, seed):
    """Selection-biased validation comparison of an artifact against the baselines."""
    names = [f["name"] for f in manifest["feature_set"]["features"]]
    Xv, yv, _, gv = matrices(validation, names)
    p = predict_proba(artifact, Xv)
    return compare(p, validation, names, yv, gv, seed=seed) | {
        "selection_biased": True,
        "note": "validation compares families/configurations; only a once-evaluated holdout "
        "is unbiased",
    }


def train(
    registry,
    dataset_id,
    *,
    family,
    split,
    grid=None,
    n_folds=4,
    seed=0,
    max_configs=24,
    deadline_seconds=900.0,
    calibration="platt",
    clock=time.monotonic,
):
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}")
    if calibration not in CALIBRATIONS:
        raise ValueError(f"calibration must be one of {CALIBRATIONS}")
    manifest = registry.dataset(dataset_id)
    pin = splits.guard(registry, dataset_id, split, pin_if_missing=True)
    rows = registry.dataset_rows(dataset_id)
    development, validation = splits.development_and_validation(rows, split)
    if len(development) < MIN_DEVELOPMENT_ROWS or len(validation) < MIN_EVALUATION_ROWS:
        raise ValueError(
            f"insufficient rows: development {len(development)} (≥{MIN_DEVELOPMENT_ROWS}),"
            f" validation {len(validation)} (≥{MIN_EVALUATION_ROWS})"
        )
    artifact, result, grid = fit(
        manifest,
        dataset_id,
        development,
        family=family,
        split=split,
        grid=grid,
        n_folds=n_folds,
        seed=seed,
        max_configs=max_configs,
        deadline_seconds=deadline_seconds,
        calibration=calibration,
        clock=clock,
    )
    model_id = registry.register_model(
        artifact.to_json(),
        family=family,
        dataset_id=dataset_id,
        summary={
            "feature_set": artifact.training["feature_set"],
            "selected_config": result.selected_config,
        },
    )
    metrics_ = validation_metrics(artifact, manifest, validation, seed=seed)
    evaluation_id = registry.record_evaluation(
        model_id, dataset_id, "validation:" + split.key, metrics_, selection_biased=True
    )
    experiment_id = registry.record_experiment(
        "train",
        {
            "family": family,
            "grid": grid,
            "split": split.to_dict(),
            "seed": seed,
            "n_folds": n_folds,
            "max_configs": max_configs,
            "calibration": calibration,
        },
        {"model_id": model_id, "evaluation_id": evaluation_id, "search": result.summary()},
        dataset_id=dataset_id,
    )
    return {
        "status": "complete",
        "model_id": model_id,
        "experiment_id": experiment_id,
        "evaluation_id": evaluation_id,
        "family": family,
        "selected_config": result.selected_config,
        "search_stop_reason": result.stop_reason,
        "configs_tried": len(result.results),
        "development_rows": len(development),
        "validation": metrics_,
        "holdout": registry.holdout_status(dataset_id, f"from={pin}"),
    }


def evaluate_holdout(registry, dataset_id, model_id, split, *, seed=0):
    """The one-time final evaluation: recorded in the holdout ledger (consumed, or a reuse
    that is never reported as untouched)."""
    from ..models.artifact import ModelArtifact as _Artifact

    manifest = registry.dataset(dataset_id)
    names = [f["name"] for f in manifest["feature_set"]["features"]]
    artifact = _Artifact.from_dict(__import__("json").loads(registry.model_artifact(model_id)))
    if artifact.training.get("dataset_id") != dataset_id:
        raise ValueError("model was trained on another dataset")
    if artifact.training.get("split", {}).get("key") != split.key:
        raise ValueError("holdout split differs from the model's training split")
    key = splits.holdout_key(registry, dataset_id, split)
    if registry.holdout_evaluation(model_id, dataset_id, key) is not None:
        raise ValueError("this model was already evaluated on this holdout")
    rows = splits.holdout_rows(registry.dataset_rows(dataset_id), split)
    if len(rows) < MIN_EVALUATION_ROWS:
        raise ValueError(f"insufficient holdout rows: {len(rows)}")
    X, y, _, groups = matrices(rows, names)
    holdout_metrics = compare(predict_proba(artifact, X), rows, names, y, groups, seed=seed)
    status = registry.evaluate_holdout(model_id, dataset_id, key, holdout_metrics)
    return {"status": "complete", "ledger": status, "holdout": holdout_metrics}
