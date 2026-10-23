"""Probability metrics and seeded group bootstrap intervals (float, not Decimal).

Conventions follow `evaluation.scoring`: single-Bernoulli Brier without a factor of
two; natural-log loss with probabilities clipped to [1e-6, 1 - 1e-6] for the loss only,
reporting how many were clipped.
"""

import numpy as np

from ._common import as_groups, as_labels, as_probabilities, as_seed

EPSILON = 1e-6


def brier(p, y):
    p = as_probabilities(p)
    y = _labels(y, p)
    return float(np.mean((p - y) ** 2))


def log_loss(p, y):
    """(mean natural-log loss, number of probabilities changed by clipping)."""
    p = as_probabilities(p)
    y = _labels(y, p)
    q = np.clip(p, EPSILON, 1 - EPSILON)
    loss = -np.mean(np.where(y == 1, np.log(q), np.log1p(-q)))
    return float(loss), int(np.count_nonzero(q != p))


def calibration_table(p, y, bins=10):
    """Equal-width bins on [0, 1] (p == 1 falls in the last bin); empty bins have None means."""
    p = as_probabilities(p)
    y = _labels(y, p)
    if type(bins) is not int or bins < 1:
        raise ValueError("bins must be a positive int")
    index = np.minimum((p * bins).astype(np.int64), bins - 1)
    table = []
    for b in range(bins):
        mask = index == b
        count = int(mask.sum())
        table.append(
            {
                "bin": b,
                "low": b / bins,
                "high": (b + 1) / bins,
                "count": count,
                "mean_p": float(p[mask].mean()) if count else None,
                "mean_y": float(y[mask].mean()) if count else None,
            }
        )
    return table


def ece(p, y, bins=10):
    """Expected calibration error: count-weighted mean |mean p - mean y| over the table."""
    table = calibration_table(p, y, bins)
    n = sum(row["count"] for row in table)
    return float(
        sum(row["count"] / n * abs(row["mean_p"] - row["mean_y"]) for row in table if row["count"])
    )


METRICS = {"brier": brier, "log_loss": lambda p, y: log_loss(p, y)[0], "ece": ece}


def grouped_bootstrap(metric, p, y, groups, *, n=1000, seed, level=0.95):
    """(estimate, low, high): percentile interval from resampling whole groups.

    `metric` is a name in METRICS or a callable (p, y) -> float. Each replicate draws
    as many groups as there are, with replacement, and keeps every row of each draw.
    """
    p = as_probabilities(p)
    y = _labels(y, p)
    fn = _metric(metric)

    def statistic(index):
        return fn(p[index], y[index])

    return _bootstrap(statistic, len(p), groups, n, seed, level)


def paired_bootstrap(metric, p_a, p_b, y, groups, *, n=1000, seed, level=0.95):
    """(estimate, low, high) of metric(p_a) - metric(p_b) on identical group resamples."""
    p_a = as_probabilities(p_a)
    p_b = as_probabilities(p_b, len(p_a))
    y = _labels(y, p_a)
    fn = _metric(metric)

    def statistic(index):
        return fn(p_a[index], y[index]) - fn(p_b[index], y[index])

    return _bootstrap(statistic, len(p_a), groups, n, seed, level)


def _bootstrap(statistic, n_rows, groups, n, seed, level):
    groups = as_groups(groups, n_rows)
    if type(n) is not int or n < 1:
        raise ValueError("n must be a positive int")
    if not 0 < level < 1:
        raise ValueError("level must be in (0, 1)")
    _, codes = np.unique(groups, return_inverse=True)
    order = np.argsort(codes, kind="stable")
    members = np.split(order, np.cumsum(np.bincount(codes))[:-1])
    if len(members) < 2:
        raise ValueError("bootstrap needs at least two groups")
    rng = np.random.Generator(np.random.PCG64(as_seed(seed)))
    replicates = np.empty(n)
    for r in range(n):
        draws = rng.integers(0, len(members), size=len(members))
        replicates[r] = statistic(np.concatenate([members[g] for g in draws]))
    tail = (1 - level) / 2
    low, high = np.quantile(replicates, [tail, 1 - tail])
    return float(statistic(np.arange(n_rows))), float(low), float(high)


def _labels(y, p):
    if len(p) == 0:
        raise ValueError("metrics need at least one row")
    return as_labels(y, len(p))


def _metric(metric):
    if callable(metric):
        return metric
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; expected one of {sorted(METRICS)}")
    return METRICS[metric]
