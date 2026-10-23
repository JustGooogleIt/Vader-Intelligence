"""Platt calibration of raw expression scores and natural-log loss.

`platt_fit` sees training scores and training labels only. Probabilities are clipped to
[1e-6, 1 - 1e-6] for loss computation only; the clip count is returned, stored
probabilities are never modified.
"""

import numpy as np

from .expressions import CONST_DIGITS, round_sig

EPSILON = 1e-6
L2 = 1e-6
MAX_ITERATIONS = 100
TOLERANCE = 1e-10
SLACK = 1e-12  # objective increase attributed to rounding, accepted near the optimum


def sigmoid(z):
    return np.exp(-np.logaddexp(0.0, -z))


def probabilities(scores, a, b):
    """The single probability formula used in search and replay: sigmoid(a*s + b)."""
    return sigmoid(a * scores + b)


def log_loss_rows(p, y):
    """Per-row natural-log loss on clipped probabilities, and the number of clipped rows."""
    q = np.clip(p, EPSILON, 1 - EPSILON)
    clipped = int(np.count_nonzero(q != p))
    return -np.log(np.where(y == 1, q, 1 - q)), clipped


def platt_fit(scores, y):
    """(a, b) maximizing the L2-penalized Bernoulli likelihood of sigmoid(a*s + b), or None.

    Newton steps with step halving on standardized scores z = (s - mean) / std; the
    penalty L2/2 * (a_z^2 + b_z^2) is added to the mean log loss, which keeps the problem
    strictly convex even for separable data. Failure (zero spread, singular or non-finite
    step, no decrease, no convergence within MAX_ITERATIONS) returns None. The returned
    coefficients are on the raw-score scale, rounded to 12 significant digits.
    """
    mean, spread = float(np.mean(scores)), float(np.std(scores))
    if not (np.isfinite(spread) and spread > 0):
        return None
    z = (scores - mean) / spread
    target = y.astype(np.float64)

    def objective(w):
        eta = w[0] * z + w[1]
        return float(np.mean(np.logaddexp(0.0, eta) - target * eta) + 0.5 * L2 * (w @ w))

    w = np.zeros(2)
    current = objective(w)
    for _ in range(MAX_ITERATIONS):
        p = sigmoid(w[0] * z + w[1])
        residual, weight = p - target, p * (1 - p)
        g0, g1 = np.mean(residual * z) + L2 * w[0], np.mean(residual) + L2 * w[1]
        h00, h01, h11 = np.mean(weight * z * z) + L2, np.mean(weight * z), np.mean(weight) + L2
        det = h00 * h11 - h01 * h01
        if not (np.isfinite(det) and det > 0):
            return None
        step = np.array([(h11 * g0 - h01 * g1) / det, (h00 * g1 - h01 * g0) / det])
        if not np.isfinite(step).all():
            return None
        if np.max(np.abs(step)) < TOLERANCE:
            break
        scale = 1.0
        while True:
            trial = w - scale * step
            value = objective(trial)
            if value <= current + SLACK:
                break
            scale /= 2
            if scale < 1e-10:
                return None
        w, current = trial, value
    else:
        return None
    a, b = w[0] / spread, w[1] - w[0] * mean / spread
    if not (np.isfinite(a) and np.isfinite(b)):
        return None
    return round_sig(a, CONST_DIGITS), round_sig(b, CONST_DIGITS)
