"""Synthetic chronological datasets: games (groups) with several dated snapshot rows."""

import numpy as np

from vader_intelligence.models.preprocess import FeatureSpec

NAMES = ["strength", "noisy", "mid"]
SPECS = [
    FeatureSpec("strength"),
    FeatureSpec("noisy", missing="zero_indicator"),
    FeatureSpec("mid", missing="forbid", scale=False),
]


def sigmoid(z):
    return 1 / (1 + np.exp(-z))


def games(n_groups=120, rows_per_group=4, days=150, seed=0):
    """Rows of one game share its label; snapshots fall on game day or the day before."""
    rng = np.random.default_rng(seed)
    game_day = np.sort(rng.integers(0, days, n_groups))
    n = n_groups * rows_per_group
    groups = np.repeat(np.array([f"game-{i:03d}" for i in range(n_groups)]), rows_per_group)
    offset = np.tile(np.arange(rows_per_group) % 2, n_groups)
    day = np.repeat(game_day, rows_per_group) - offset
    dates = np.datetime64("2026-04-01") + day.astype("timedelta64[D]")
    strength = rng.normal(size=n_groups)
    y = np.repeat((rng.random(n_groups) < sigmoid(1.5 * strength)).astype(np.int64), rows_per_group)
    latent = np.repeat(strength, rows_per_group)
    noisy = rng.normal(size=n)
    noisy[rng.random(n) < 0.2] = np.nan
    mid = np.clip(sigmoid(1.5 * latent + rng.normal(scale=0.3, size=n)), 0.01, 0.99)
    X = np.column_stack([latent + rng.normal(scale=0.5, size=n), noisy, mid])
    return X, y, dates, groups


class StepClock:
    """Injected monotonic clock that advances by `step` seconds per reading."""

    def __init__(self, step=1.0):
        self.now, self.step, self.calls = 0.0, step, 0

    def __call__(self):
        self.now += self.step
        self.calls += 1
        return self.now
