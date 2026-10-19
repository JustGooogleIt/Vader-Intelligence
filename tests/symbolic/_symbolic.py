"""Synthetic data only: y ~ Bernoulli(sigmoid(2*x0 - x1)), x2 is noise. No archive data."""

import numpy as np

NAMES = ["x0", "x1", "x2"]


def planted(rows, seed):
    rng = np.random.Generator(np.random.PCG64(seed))
    X = rng.normal(size=(rows, 3))
    p = 1 / (1 + np.exp(-(2 * X[:, 0] - X[:, 1])))
    return X, (rng.random(rows) < p).astype(np.int64)


class TickClock:
    """Monotonic test clock: each call returns the previous value plus one second."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return float(self.calls - 1)
