import json

import numpy as np
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from vader_intelligence.models import gbm, logistic
from vader_intelligence.provenance import json_text

GBM = dict(
    learning_rate=0.2, max_iter=25, max_leaf_nodes=7, min_samples_leaf=5, l2_regularization=0.1
)
NAN_SPLIT = dict(GBM, learning_rate=0.3, max_iter=5, max_leaf_nodes=3)


def linear_data(seed=0, n=300):
    rng = np.random.default_rng(seed)
    Z = rng.normal(size=(n, 3))
    y = (rng.random(n) < 1 / (1 + np.exp(-(1.5 * Z[:, 0] - Z[:, 1])))).astype(np.int64)
    return Z, y


def missing_data(seed=0, n=500):
    """NaN in two columns; missingness of column 0 carries signal."""
    rng = np.random.default_rng(seed)
    Z = rng.normal(size=(n, 3))
    missing = rng.random(n) < 0.35
    Z[missing, 0] = np.nan
    Z[rng.random(n) < 0.1, 2] = np.nan
    signal = np.where(missing, 1.2, np.nan_to_num(Z[:, 0])) + 0.8 * Z[:, 1]
    y = (rng.random(n) < 1 / (1 + np.exp(-signal))).astype(np.int64)
    return Z, y


def nan_split_data(seed=1, n=400):
    """Only missingness is predictive: scikit-learn splits NaN vs value (threshold +inf)."""
    rng = np.random.default_rng(seed)
    Z = rng.normal(size=(n, 2))
    missing = rng.random(n) < 0.5
    Z[missing, 0] = np.nan
    return Z, (missing ^ (rng.random(n) < 0.1)).astype(np.int64)


def round_trip(params):
    return json.loads(json_text(params))


def test_logistic_export_round_trip_and_sklearn_parity():
    Z, y = linear_data()
    model = LogisticRegression(C=0.5, l1_ratio=0.0, solver="lbfgs", max_iter=1000, tol=1e-8)
    model.fit(Z, y)
    params = round_trip(logistic.export(model, Z))
    fresh = np.random.default_rng(9).normal(scale=3.0, size=(200, 3))
    assert np.max(np.abs(logistic.logit(params, fresh) - model.decision_function(fresh))) <= 1e-9
    assert float(params["parity_max_abs_error"]) <= 1e-9


def test_logistic_determinism_and_validation():
    Z, y = linear_data(1)
    first = logistic.fit_logistic(Z, y, C=1.0, seed=3)
    assert json_text(first) == json_text(logistic.fit_logistic(Z, y, C=1.0, seed=3))
    assert set(first) == {"coef", "intercept", "parity_max_abs_error"} and len(first["coef"]) == 3
    with pytest.raises(ValueError, match="both classes"):
        logistic.fit_logistic(Z, np.zeros(len(Z), dtype=int), C=1.0, seed=0)
    with pytest.raises(ValueError, match="dtype"):
        logistic.fit_logistic(Z, y.astype(float), C=1.0, seed=0)
    with pytest.raises(ValueError, match="NaN"):
        logistic.fit_logistic(np.where(Z > 2, np.nan, Z), y, C=1.0, seed=0)
    with pytest.raises(ValueError, match="columns"):
        logistic.logit(first, Z[:, :2])
    with pytest.raises(ValueError):
        logistic.logit(first | {"parity_max_abs_error": "0.0001"}, Z)


@pytest.mark.parametrize(
    "data, settings",
    [
        (missing_data(), GBM),
        (nan_split_data(), NAN_SPLIT),
    ],
)
def test_gbm_parity_including_nan_routing(data, settings):
    Z, y = data
    model = HistGradientBoostingClassifier(
        early_stopping=False, random_state=0, categorical_features=None, **settings
    ).fit(Z, y)
    params = round_trip(gbm.export(model, Z))
    assert float(params["parity_max_abs_error"]) <= 1e-9
    fresh = np.random.default_rng(5).normal(scale=2.0, size=(400, Z.shape[1]))
    fresh[np.random.default_rng(6).random(fresh.shape) < 0.3] = np.nan  # incl. never-NaN columns
    fresh[:5] = np.nan
    error = np.max(np.abs(gbm.raw_score(params, fresh) - model.decision_function(fresh)))
    assert error <= 1e-9
    inner = [
        (route, threshold)
        for tree in params["trees"]
        for route, threshold, leaf in zip(
            tree["missing_go_to_left"], tree["num_threshold"], tree["is_leaf"]
        )
        if not leaf
    ]
    assert {route for route, _ in inner} == {0, 1} or "inf" in {t for _, t in inner}


def test_gbm_nan_split_exports_infinite_threshold():
    Z, y = nan_split_data()
    params = round_trip(gbm.fit_gbm(Z, y, seed=0, **NAN_SPLIT))
    root = params["trees"][0]
    assert root["feature_idx"][0] == 0 and root["num_threshold"][0] == "inf"
    assert root["missing_go_to_left"][0] == 0 and root["is_leaf"][root["left"][0]] == 1
    first_tree = params | {"trees": params["trees"][:1]}
    raw = gbm.raw_score(first_tree, np.array([[np.nan, 0.0], [1e300, 0.0], [-1e300, 0.0]]))
    assert raw[1] == raw[2] != raw[0]  # every finite value goes left of +inf, NaN goes right


def test_gbm_determinism_and_round_trip():
    Z, y = missing_data(1)
    first = gbm.fit_gbm(Z, y, seed=7, **GBM)
    assert json_text(first) == json_text(gbm.fit_gbm(Z, y, seed=7, **GBM))
    assert len(first["trees"]) == GBM["max_iter"] and first["n_features"] == 3
    np.testing.assert_array_equal(gbm.raw_score(round_trip(first), Z), gbm.raw_score(first, Z))


def test_gbm_refuses_tampered_trees():
    Z, y = missing_data(2)
    params = gbm.fit_gbm(Z, y, seed=0, **GBM)
    cyclic = round_trip(params)
    inner = cyclic["trees"][0]["is_leaf"].index(0)
    cyclic["trees"][0]["left"][inner] = inner  # would loop forever
    with pytest.raises(ValueError, match="structure"):
        gbm.raw_score(cyclic, Z)
    short = round_trip(params)
    short["trees"][0]["value"].pop()
    with pytest.raises(ValueError, match="equal length"):
        gbm.raw_score(short, Z)
    with pytest.raises(ValueError, match="columns"):
        gbm.raw_score(params, Z[:, :2])


def test_gbm_export_fails_loudly_on_unsupported_models_and_internals():
    Z, y = missing_data(3)
    kwargs = dict(early_stopping=False, random_state=0, **GBM)
    categorical = np.column_stack([np.random.default_rng(0).integers(0, 3, len(Z)), Z[:, 1:]])
    model = HistGradientBoostingClassifier(categorical_features=[0], **kwargs)
    with pytest.raises(RuntimeError, match="unsupported scikit-learn"):
        gbm.export(model.fit(categorical, y), categorical)
    three = HistGradientBoostingClassifier(categorical_features=None, **kwargs)
    with pytest.raises(ValueError, match="binary"):
        gbm.export(three.fit(Z, np.arange(len(Z)) % 3), Z)
    fitted = HistGradientBoostingClassifier(categorical_features=None, **kwargs).fit(Z, y)
    fitted._predictors[0] = fitted._predictors[0] * 2  # e.g. trees per iteration changed
    with pytest.raises(RuntimeError, match="exactly one tree"):
        gbm.export(fitted, Z)
    fitted = HistGradientBoostingClassifier(categorical_features=None, **kwargs).fit(Z, y)
    fitted._baseline_prediction = fitted._baseline_prediction.ravel()
    with pytest.raises(RuntimeError, match="_baseline_prediction"):
        gbm.export(fitted, Z)
    fitted = HistGradientBoostingClassifier(categorical_features=None, **kwargs).fit(Z, y)
    del fitted._predictors
    with pytest.raises(RuntimeError, match="missing _predictors"):
        gbm.export(fitted, Z)
