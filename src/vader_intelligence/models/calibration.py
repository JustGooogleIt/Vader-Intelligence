"""Calibration maps from raw log-odds to probabilities, stored as plain data.

Every family emits raw log-odds s, and `apply(cal, s)` always takes s:
identity -> sigmoid(s); platt -> sigmoid(a*s + b); isotonic -> interp(sigmoid(s)),
where the isotonic map was fit by `fit_isotonic` on the uncalibrated sigmoid(s).
Calibrators are fit only on the scores and labels passed in (out-of-fold, by contract).
"""

import numpy as np
from sklearn.isotonic import IsotonicRegression

from ._common import (
    PARITY_TOLERANCE,
    as_labels,
    as_probabilities,
    as_vector,
    decode,
    decode_array,
    encode,
    encode_array,
    sigmoid,
)

ISOTONIC_MIN, ISOTONIC_MAX = 1e-6, 1 - 1e-6
KEYS = {
    "identity": {"kind"},
    "platt": {"kind", "a", "b", "l2", "n_iter"},
    "isotonic": {"kind", "x", "y", "parity_max_abs_error"},
}


def identity():
    return {"kind": "identity"}


def fit_platt(scores, y, *, l2=1e-6, max_iter=100):
    """Newton's method on mean log loss + l2/2 * (a^2 + b^2), from (a, b) = (0, 0).

    Stops when the (affine-invariant) Newton decrement falls below 1e-20; raises if
    that does not happen within `max_iter` steps. Backtracking line search far from
    the optimum, full steps near it (where loss differences fall below rounding).
    """
    s = as_vector(scores, name="scores")
    y = as_labels(y, len(s), both_classes=True)
    if isinstance(l2, bool) or not 0 < float(l2) < np.inf:
        raise ValueError("l2 must be a positive finite number")
    l2 = float(l2)
    design = np.column_stack([s, np.ones_like(s)])

    def objective(theta):
        z = design @ theta
        return float(np.mean(np.logaddexp(0.0, z) - y * z) + 0.5 * l2 * theta @ theta)

    theta = np.zeros(2)
    value = objective(theta)
    for n_iter in range(max_iter + 1):
        p = sigmoid(design @ theta)
        grad = design.T @ (p - y) / len(s) + l2 * theta
        hess = (design.T * (p * (1 - p))) @ design / len(s) + l2 * np.eye(2)
        step = np.linalg.solve(hess, grad)
        decrement = float(grad @ step)
        if decrement <= 1e-20:
            break
        if n_iter == max_iter:
            raise ValueError(f"Platt scaling did not converge in {max_iter} Newton steps")
        t = 1.0
        if decrement > 1e-10:
            while objective(theta - t * step) > value - 0.25 * t * decrement:
                t /= 2
                if t < 1e-12:
                    raise ValueError("Platt scaling line search failed")
        theta = theta - t * step
        value = objective(theta)
    return {
        "kind": "platt",
        "a": encode(theta[0]),
        "b": encode(theta[1]),
        "l2": encode(l2),
        "n_iter": n_iter,
    }


def fit_isotonic(probabilities, y):
    """Isotonic map on uncalibrated probabilities, bounded to [1e-6, 1 - 1e-6].

    Exported as threshold arrays; inference is `numpy.interp`, which clips outside the
    training range exactly like out_of_bounds="clip". Parity is asserted and recorded.
    """
    p = as_probabilities(probabilities)
    y = as_labels(y, len(p), both_classes=True)
    model = IsotonicRegression(
        out_of_bounds="clip", y_min=ISOTONIC_MIN, y_max=ISOTONIC_MAX, increasing=True
    ).fit(p, y)
    cal = {
        "kind": "isotonic",
        "x": encode_array(model.X_thresholds_),
        "y": encode_array(model.y_thresholds_),
    }
    error = float(np.max(np.abs(_interp(cal, p) - model.predict(p)), initial=0.0))
    if not error <= PARITY_TOLERANCE:
        raise RuntimeError(f"isotonic export parity {error!r} exceeds {PARITY_TOLERANCE}")
    cal["parity_max_abs_error"] = encode(error)
    return cal


def apply(cal, raw):
    """Calibrated probabilities from raw log-odds (see module docstring)."""
    if not isinstance(cal, dict) or cal.get("kind") not in KEYS:
        raise ValueError(f"unknown calibration: {cal!r}")
    if set(cal) != KEYS[cal["kind"]]:
        raise ValueError(f"malformed {cal['kind']} calibration")
    raw = as_vector(raw, name="raw scores")
    if cal["kind"] == "identity":
        return sigmoid(raw)
    if cal["kind"] == "platt":
        decode(cal["l2"])
        if type(cal["n_iter"]) is not int or cal["n_iter"] < 0:
            raise ValueError("platt n_iter must be a nonnegative int")
        return sigmoid(decode(cal["a"]) * raw + decode(cal["b"]))
    if not decode(cal["parity_max_abs_error"]) <= PARITY_TOLERANCE:
        raise ValueError("recorded isotonic parity exceeds tolerance")
    return _interp(cal, sigmoid(raw))


def _interp(cal, p):
    x, y = decode_array(cal["x"]), decode_array(cal["y"])
    if len(x) == 0 or len(x) != len(y):
        raise ValueError("isotonic thresholds must be nonempty and of equal length")
    if (np.diff(x) <= 0).any() or x[0] < 0 or x[-1] > 1:
        raise ValueError("isotonic x thresholds must increase strictly within [0, 1]")
    if (np.diff(y) < 0).any() or y[0] < ISOTONIC_MIN or y[-1] > ISOTONIC_MAX:
        raise ValueError("isotonic y thresholds must be nondecreasing within the bounds")
    return np.interp(p, x, y)
