"""Binary histogram gradient boosting: scikit-learn fit, tree export, numpy traversal.

Export reads scikit-learn private state (`_predictors`, each `TreePredictor.nodes`
structured array, `_baseline_prediction`). If that state changes shape the export fails
loudly; it never emits trees that were not checked against `decision_function`.
"""

import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier

from ._common import (
    PARITY_TOLERANCE,
    as_labels,
    as_matrix,
    as_seed,
    decode,
    decode_array,
    encode,
    encode_array,
    single_threaded,
)

INT_FIELDS = ("feature_idx", "missing_go_to_left", "left", "right", "is_leaf")
FLOAT_FIELDS = ("num_threshold", "value")
KEYS = {"baseline", "n_features", "parity_max_abs_error", "trees"}


def fit_gbm(
    Z,
    y,
    *,
    learning_rate,
    max_iter,
    max_leaf_nodes,
    min_samples_leaf,
    l2_regularization,
    max_bins=255,
    seed,
):
    """Fit on these (training) rows only (NaN allowed); returns exported params."""
    Z = as_matrix(Z, allow_nan=True, name="Z")
    y = as_labels(y, len(Z), both_classes=True)
    model = HistGradientBoostingClassifier(
        learning_rate=learning_rate,
        max_iter=max_iter,
        max_leaf_nodes=max_leaf_nodes,
        min_samples_leaf=min_samples_leaf,
        l2_regularization=l2_regularization,
        max_bins=max_bins,
        early_stopping=False,
        categorical_features=None,
        random_state=as_seed(seed),
    )
    with single_threaded():
        model.fit(Z, y)
    return export(model, Z)


def export(model, Z):
    """Export every tree and the baseline; asserts numpy parity with `decision_function` on Z.

    Params: {"baseline", "n_features", "trees": [{field: [...]}, ...],
    "parity_max_abs_error"}; tree node arrays use node 0 as root, children after parents.
    """
    Z = as_matrix(Z, allow_nan=True, name="Z")
    if not isinstance(model, HistGradientBoostingClassifier):
        raise ValueError("expected a fitted HistGradientBoostingClassifier")
    per_iteration = getattr(model, "n_trees_per_iteration_", None)
    if list(getattr(model, "classes_", [])) != [0, 1] or per_iteration != 1:
        raise ValueError("only binary classification on labels {0, 1} is supported")
    if model.n_features_in_ != Z.shape[1]:
        raise ValueError(f"Z has {Z.shape[1]} columns, model has {model.n_features_in_}")
    categorical = getattr(model, "is_categorical_", None)
    if getattr(model, "_preprocessor", None) is not None or categorical is not None:
        raise _unsupported("categorical features are not supported")
    predictors = getattr(model, "_predictors", None)
    baseline = getattr(model, "_baseline_prediction", None)
    if not isinstance(predictors, list) or not isinstance(baseline, np.ndarray):
        raise _unsupported("missing _predictors or _baseline_prediction")
    if baseline.shape != (1, 1):
        raise _unsupported(f"_baseline_prediction has shape {baseline.shape}")
    trees = []
    for i, stage in enumerate(predictors):
        if not isinstance(stage, list) or len(stage) != 1:
            raise _unsupported(f"iteration {i} does not hold exactly one tree")
        nodes = getattr(stage[0], "nodes", None)
        names = getattr(getattr(nodes, "dtype", None), "names", None) or ()
        required = INT_FIELDS + FLOAT_FIELDS + ("is_categorical",)
        if not set(required) <= set(names) or nodes.ndim != 1 or len(nodes) == 0:
            raise _unsupported(f"tree {i} nodes are not the expected structured array")
        if nodes["is_categorical"].any():
            raise _unsupported(f"tree {i} has a categorical split")
        tree = {field: [int(v) for v in nodes[field]] for field in INT_FIELDS}
        tree["num_threshold"] = encode_array(nodes["num_threshold"], allow_inf=True)
        tree["value"] = encode_array(nodes["value"])
        trees.append(tree)
    params = {"baseline": encode(baseline[0, 0]), "n_features": Z.shape[1], "trees": trees}
    try:
        exported = _raw_score(params, Z)
    except ValueError as exc:  # e.g. node order no longer parent-before-children
        raise _unsupported(str(exc)) from exc
    error = float(np.max(np.abs(exported - model.decision_function(Z)), initial=0.0))
    if not error <= PARITY_TOLERANCE:
        raise RuntimeError(f"gbm export parity {error!r} exceeds {PARITY_TOLERANCE}")
    params["parity_max_abs_error"] = encode(error)
    return params


def raw_score(params, Z):
    """Raw log-odds: baseline plus every tree's leaf value, in tree order.

    A row goes left iff x <= num_threshold; NaN goes left iff missing_go_to_left.
    """
    if not isinstance(params, dict) or set(params) != KEYS:
        raise ValueError("malformed gbm params")
    if not decode(params["parity_max_abs_error"]) <= PARITY_TOLERANCE:
        raise ValueError("recorded gbm export parity exceeds tolerance")
    return _raw_score(params, Z)


def _raw_score(params, Z):
    n_features = params["n_features"]
    if type(n_features) is not int or n_features < 1:
        raise ValueError("n_features must be a positive int")
    if not isinstance(params["trees"], list):
        raise ValueError("trees must be a list")
    Z = as_matrix(Z, allow_nan=True, name="Z")
    if Z.shape[1] != n_features:
        raise ValueError(f"Z has {Z.shape[1]} columns, model expects {n_features}")
    trees = [_decode_tree(tree, n_features) for tree in params["trees"]]
    raw = np.zeros(len(Z)) + decode(params["baseline"])
    for tree in trees:
        raw += _tree_values(tree, Z)
    return raw


def _tree_values(tree, Z):
    node = np.zeros(len(Z), dtype=np.int64)
    active = np.flatnonzero(tree["is_leaf"][node] == 0)
    while active.size:  # terminates: children always have larger indices than parents
        current = node[active]
        x = Z[active, tree["feature_idx"][current]]
        go_left = np.where(
            np.isnan(x),
            tree["missing_go_to_left"][current] == 1,
            x <= tree["num_threshold"][current],
        )
        node[active] = np.where(go_left, tree["left"][current], tree["right"][current])
        active = active[tree["is_leaf"][node[active]] == 0]
    return tree["value"][node]


def _decode_tree(tree, n_features):
    if not isinstance(tree, dict) or set(tree) != set(INT_FIELDS + FLOAT_FIELDS):
        raise ValueError("malformed gbm tree")
    size = len(tree["value"]) if isinstance(tree["value"], list) else 0
    if size == 0 or any(not isinstance(tree[f], list) or len(tree[f]) != size for f in tree):
        raise ValueError("gbm tree arrays must be nonempty lists of equal length")
    decoded = {}
    for field in INT_FIELDS:
        if any(type(v) is not int for v in tree[field]):
            raise ValueError(f"gbm tree field {field} must hold ints")
        decoded[field] = np.array(tree[field], dtype=np.int64)
    decoded["num_threshold"] = decode_array(tree["num_threshold"], allow_inf=True)
    decoded["value"] = decode_array(tree["value"])
    index = np.arange(size)
    inner = decoded["is_leaf"] == 0
    ok = (
        np.isin(decoded["is_leaf"], (0, 1)).all()
        and np.isin(decoded["missing_go_to_left"], (0, 1)).all()
        and (
            (decoded["feature_idx"][inner] >= 0) & (decoded["feature_idx"][inner] < n_features)
        ).all()
        and all(
            ((decoded[c][inner] > index[inner]) & (decoded[c][inner] < size)).all()
            for c in ("left", "right")
        )
    )
    if not ok:
        raise ValueError("gbm tree structure is invalid")
    return decoded


def _unsupported(reason):
    return RuntimeError(
        f"unsupported scikit-learn {sklearn.__version__} HistGradientBoosting internals: {reason}"
    )
