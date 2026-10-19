"""L2 logistic regression: scikit-learn fit, decimal-string export, numpy inference."""

import numpy as np
from sklearn.linear_model import LogisticRegression

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

KEYS = {"coef", "intercept", "parity_max_abs_error"}


def fit_logistic(Z, y, *, C, max_iter=1000, tol=1e-8, seed):
    """Fit on these (training) rows only; returns exported params (see `export`)."""
    Z = as_matrix(Z, allow_nan=False, name="Z")
    y = as_labels(y, len(Z), both_classes=True)
    if isinstance(C, bool) or not 0 < float(C) < np.inf:
        raise ValueError("C must be a positive finite number")
    # l1_ratio=0 is scikit-learn >= 1.8's spelling of penalty="l2" (now deprecated).
    model = LogisticRegression(
        C=float(C),
        l1_ratio=0.0,
        solver="lbfgs",
        max_iter=max_iter,
        tol=tol,
        random_state=as_seed(seed),
    )
    with single_threaded():
        model.fit(Z, y)
    return export(model, Z)


def export(model, Z):
    """Export a fitted binary LogisticRegression; asserts numpy parity on Z."""
    Z = as_matrix(Z, allow_nan=False, name="Z")
    if not isinstance(model, LogisticRegression) or list(model.classes_) != [0, 1]:
        raise ValueError("expected a LogisticRegression fitted on labels {0, 1}")
    coef = np.asarray(model.coef_, dtype=np.float64)
    if coef.shape != (1, Z.shape[1]):
        raise ValueError(f"unexpected coefficient shape {coef.shape}")
    params = {"coef": encode_array(coef[0]), "intercept": encode(model.intercept_[0])}
    error = float(np.max(np.abs(_logit(params, Z) - model.decision_function(Z)), initial=0.0))
    if not error <= PARITY_TOLERANCE:
        raise RuntimeError(f"logistic export parity {error!r} exceeds {PARITY_TOLERANCE}")
    params["parity_max_abs_error"] = encode(error)
    return params


def logit(params, Z):
    """Raw log-odds Z @ coef + intercept."""
    if not isinstance(params, dict) or set(params) != KEYS:
        raise ValueError("malformed logistic params")
    if not decode(params["parity_max_abs_error"]) <= PARITY_TOLERANCE:
        raise ValueError("recorded logistic export parity exceeds tolerance")
    return _logit(params, Z)


def _logit(params, Z):
    coef = decode_array(params["coef"])
    Z = as_matrix(Z, allow_nan=False, name="Z")
    if Z.shape[1] != len(coef):
        raise ValueError(f"Z has {Z.shape[1]} columns, model expects {len(coef)}")
    return Z @ coef + decode(params["intercept"])
