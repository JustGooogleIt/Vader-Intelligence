"""Folds -> grid search -> OOF Platt -> final fit -> artifact -> holdout metrics."""

import numpy as np
import pytest

from vader_intelligence.models import artifact, baselines, calibration, metrics, search

from ._synthetic import NAMES, SPECS, games

SEED = 11
GRIDS = {
    "logistic": {"C": [0.1, 1.0]},
    "gbm": {
        "learning_rate": [0.1],
        "max_iter": [15, 30],
        "max_leaf_nodes": [5],
        "min_samples_leaf": [10],
        "l2_regularization": [1.0],
    },
}


def chronological_holdout(dates, groups, fraction=0.8):
    """Development games end before the cutoff date; holdout games start on or after it."""
    cutoff = np.sort(dates)[int(fraction * len(dates))]
    first = {g: dates[groups == g].min() for g in np.unique(groups)}
    last = {g: dates[groups == g].max() for g in np.unique(groups)}
    development = np.array([last[g] < cutoff for g in groups])
    holdout = np.array([first[g] >= cutoff for g in groups])
    return development, holdout


def pipeline(family, directory):
    X, y, dates, groups = games(n_groups=150, seed=4)
    dev, hold = chronological_holdout(dates, groups)
    result = search.grid_search(
        family,
        GRIDS[family],
        X[dev],
        y[dev],
        dates[dev],
        groups[dev],
        SPECS,
        n_folds=3,
        seed=SEED,
        max_configs=8,
        deadline_seconds=None,
        clock=None,
    )
    # The calibrator only ever sees out-of-fold scores of the selected configuration.
    cal = calibration.fit_platt(result.oof_raw, y[dev][result.oof_index])
    preprocessor, params = search.fit_model(
        family, result.selected_config, X[dev], y[dev], SPECS, seed=SEED
    )
    training = {
        "dataset_manifest_sha256": "0" * 64,
        "split": {"development_rows": int(dev.sum()), "holdout_rows": int(hold.sum())},
        "config": result.selected_config,
        "search": result.summary(),
        "seed": SEED,
        "code_hash": "synthetic-test",
        "dependencies": artifact.dependency_versions(),
    }
    model = artifact.ModelArtifact(family, preprocessor, params, cal, training)
    loaded = artifact.load(model.save(directory), expected_sha256=model.sha256)
    artifact.check_inputs(loaded, NAMES)
    in_memory = calibration.apply(cal, search.raw_scores(family, preprocessor, params, X[hold]))
    p = artifact.predict_proba(loaded, X[hold])
    np.testing.assert_array_equal(p, in_memory)  # bit-for-bit parity after save/load
    return model, p, X[hold], y[hold], groups[hold]


@pytest.mark.parametrize("family", ["logistic", "gbm"])
def test_end_to_end(family, tmp_path):
    model, p, X, y, groups = pipeline(family, tmp_path / "first")
    again, *_ = pipeline(family, tmp_path / "second")
    assert again.to_json() == model.to_json()  # byte-identical artifact on a rerun

    loss, clipped = metrics.log_loss(p, y)
    estimate, low, high = metrics.grouped_bootstrap("log_loss", p, y, groups, n=500, seed=0)
    assert estimate == loss and low < high and clipped == 0
    assert metrics.brier(p, y) < 0.25 and 0 <= metrics.ece(p, y) < 0.2

    mid = baselines.market_mid("mid")
    artifact.check_inputs(mid, ["mid"])
    p_mid = artifact.predict_proba(mid, X[:, [NAMES.index("mid")]])
    p_coin = artifact.predict_proba(baselines.constant("mid"), X[:, [NAMES.index("mid")]])
    versus_coin = metrics.paired_bootstrap("log_loss", p, p_coin, y, groups, n=500, seed=0)
    assert versus_coin[0] < 0 and versus_coin[1] <= versus_coin[2]
    versus_mid = metrics.paired_bootstrap("brier", p, p_mid, y, groups, n=500, seed=0)
    assert versus_mid[0] == pytest.approx(metrics.brier(p, y) - metrics.brier(p_mid, y))
    assert versus_mid[1] <= versus_mid[2]
