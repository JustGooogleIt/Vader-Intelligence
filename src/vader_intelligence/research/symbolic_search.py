"""Bounded symbolic search over a registry dataset (roadmap C).

The search never sees the outer validation or the holdout. Development rows are split
chronologically (by official date, with an embargo) into a search-training part and a
search-validation part; the preprocessor and the engine's Platt calibration are fit on the
search-training part only; the engine selects on the search-validation part (so its
reported metrics are selection-biased). The selected expression is registered as a
`symbolic` artifact whose probabilities equal the engine's replay (parity asserted), then
scored on the outer validation rows like any other model (still selection-biased: the
outer validation compares families). The complete search record — configuration, budget,
binding stop reason, every counter, the per-generation history and the Pareto front — is
stored as an experiment, so the winning expression is never presented as an independently
specified hypothesis. The holdout is only ever evaluated through `training.evaluate_holdout`.
"""

import json
import time
from datetime import date, timedelta

import numpy as np

from ..datasets import splits
from ..models import calibration as calibration_lib
from ..models.artifact import ModelArtifact, dependency_versions, predict_proba
from ..models.preprocess import Preprocessor
from ..provenance import source_hash
from ..symbolic import SearchConfig, replay, search
from . import training

INNER_VALIDATION_FRACTION = 0.25


def inner_split(rows, fraction, embargo_days):
    """Indices of the search-training and search-validation parts of development rows:
    the latest `fraction` of official dates (whole days) validates, after an embargo."""
    days = sorted({r["official_date"] for r in rows})
    if len(days) < 8:
        raise ValueError("too few development dates for an inner chronological split")
    cut = days[int(len(days) * (1 - fraction))]
    last_train = (date.fromisoformat(cut) - timedelta(days=embargo_days + 1)).isoformat()
    train = [i for i, r in enumerate(rows) if r["official_date"] <= last_train]
    val = [i for i, r in enumerate(rows) if r["official_date"] >= cut]
    if len(train) < training.MIN_DEVELOPMENT_ROWS // 2 or len(val) < training.MIN_EVALUATION_ROWS:
        raise ValueError("inner split leaves too few rows")
    return train, val, cut


def fit(manifest, dataset_id, development, *, split, config=None, clock=time.monotonic):
    """Search on development rows only; needs no registry (research pods call it).

    Returns `{"artifact": ModelArtifact | None, "record", "experiment_manifest", "parity"}`;
    `artifact` is None when no valid expression was found.
    """
    config = config or SearchConfig()
    if len(development) < training.MIN_DEVELOPMENT_ROWS:
        raise ValueError(f"insufficient development rows: {len(development)}")
    names = [f["name"] for f in manifest["feature_set"]["features"]]
    specs = training.feature_specs(manifest)
    X, y, _, _ = training.matrices(development, names)
    inner_train, inner_val, cut = inner_split(
        development, INNER_VALIDATION_FRACTION, split.embargo_days
    )
    preprocessor = Preprocessor.fit(X[inner_train], specs)
    Z_train = preprocessor.transform(X[inner_train])
    Z_val = preprocessor.transform(X[inner_val])
    result = search(
        Z_train,
        y[inner_train],
        Z_val,
        y[inner_val],
        list(preprocessor.output_names),
        config,
        clock=clock,
    )
    record = result.to_dict()
    experiment_manifest = {
        "kind": "symbolic_search",
        "dataset_id": dataset_id,
        "split": split.to_dict(),
        "inner_validation_from": cut,
        "inner_rows": {"train": len(inner_train), "validation": len(inner_val)},
        "config": config.to_dict(),
        "config_hash": config.config_hash(),
        "code_hash": source_hash(),
    }
    out = {"artifact": None, "record": record, "experiment_manifest": experiment_manifest}
    if result.selected is None:
        return out | {"parity": None}
    model_dict = record["selected"]["model"]
    artifact = ModelArtifact(
        "symbolic",
        preprocessor,
        {"model": model_dict},
        calibration_lib.identity(),
        {
            "dataset_id": dataset_id,
            "dataset_name": manifest["name"],
            "evidence_kind": manifest["evidence_kind"],
            "feature_set": manifest["feature_set"]["name"],
            "forward_capable": manifest["feature_set"]["forward_capable"],
            "split": split.to_dict(),
            "family": "symbolic",
            "description": record["selected"]["description"],
            "complexity": record["selected"]["complexity"],
            "search_config_hash": config.config_hash(),
            "search_stop_reason": record["stop_reason"],
            "candidates_evaluated": record["counters"]["candidates_evaluated"],
            "selection_rule": record["selection"]["rule"],
            "validation_metrics_selection_biased": True,
            "preprocessor_fit_on": "search-training part of development",
            "seed": config.seed,
            "code_hash": source_hash(),
            "dependencies": dependency_versions(),
        },
    )
    in_search = replay(model_dict, Z_val)
    parity = float(np.max(np.abs(predict_proba(artifact, X[inner_val]) - in_search)))
    if parity > 1e-9:
        raise ValueError(f"symbolic artifact does not reproduce the engine (parity {parity})")
    return out | {"artifact": artifact, "parity": parity}


def validation_metrics(artifact, manifest, validation, *, seed):
    names = [f["name"] for f in manifest["feature_set"]["features"]]
    Xv, yv, _, gv = training.matrices(validation, names)
    return training.compare(predict_proba(artifact, Xv), validation, names, yv, gv, seed=seed) | {
        "selection_biased": True,
        "note": "the engine selected on development rows and the outer validation compares "
        "families; only a once-evaluated holdout is unbiased",
    }


def run(registry, dataset_id, *, split, config=None, clock=time.monotonic):
    config = config or SearchConfig()
    manifest = registry.dataset(dataset_id)
    pin = splits.guard(registry, dataset_id, split, pin_if_missing=True)
    rows = registry.dataset_rows(dataset_id)
    development, validation = splits.development_and_validation(rows, split)
    if len(development) < training.MIN_DEVELOPMENT_ROWS:
        raise ValueError(f"insufficient development rows: {len(development)}")
    if len(validation) < training.MIN_EVALUATION_ROWS:
        raise ValueError(f"insufficient validation rows: {len(validation)}")
    fitted = fit(manifest, dataset_id, development, split=split, config=config, clock=clock)
    record, experiment_manifest = fitted["record"], fitted["experiment_manifest"]
    artifact, parity = fitted["artifact"], fitted["parity"]
    if artifact is None:
        experiment_id = registry.record_experiment(
            "symbolic_search",
            experiment_manifest,
            {"status": "no_valid_expression", "search": record},
            dataset_id=dataset_id,
        )
        return {"status": "inconclusive", "experiment_id": experiment_id, "search": _brief(record)}
    model_id = registry.register_model(
        artifact.to_json(),
        family="symbolic",
        dataset_id=dataset_id,
        summary={"description": record["selected"]["description"], "parity": parity},
    )
    metrics_ = validation_metrics(artifact, manifest, validation, seed=config.seed)
    evaluation_id = registry.record_evaluation(
        model_id, dataset_id, "validation:" + split.key, metrics_, selection_biased=True
    )
    experiment_id = registry.record_experiment(
        "symbolic_search",
        experiment_manifest,
        {
            "status": "complete",
            "model_id": model_id,
            "evaluation_id": evaluation_id,
            "parity": parity,
            "search": record,
        },
        dataset_id=dataset_id,
    )
    return {
        "status": "complete",
        "model_id": model_id,
        "experiment_id": experiment_id,
        "evaluation_id": evaluation_id,
        "expression": record["selected"]["description"],
        "complexity": record["selected"]["complexity"],
        "search": _brief(record),
        "validation": metrics_,
        "holdout": registry.holdout_status(dataset_id, f"from={pin}"),
    }


def _brief(record):
    return {
        "stop_reason": record["stop_reason"],
        "reproducible": record["reproducible"],
        "generations_completed": record["generations_completed"],
        "counters": record["counters"],
        "front_size": len(record["front"]),
        "selection": record["selection"],
    }


def search_record(registry, experiment_id):
    row = registry.db.execute(
        "SELECT result_json FROM experiments WHERE id=?", (experiment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("unknown experiment")
    return json.loads(row[0])["search"]
