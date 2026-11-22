import math

import numpy as np
import pytest

from vader_intelligence.models import metrics


def test_hand_cases():
    assert metrics.brier([0.5], [1]) == 0.25 and metrics.brier([0.5, 0.5], [0, 1]) == 0.25
    loss, clipped = metrics.log_loss([0.5, 0.5], [0, 1])
    assert loss == pytest.approx(math.log(2), abs=1e-15) and clipped == 0
    assert metrics.brier([1.0, 0.0], [1, 0]) == 0.0
    assert metrics.brier([0.2], [0]) == pytest.approx(0.04)


def test_log_loss_clips_for_the_loss_only_and_counts():
    p = np.array([0.0, 1.0, 0.5, 1e-7, 1 - 1e-7])
    original = p.copy()
    loss, clipped = metrics.log_loss(p, [1, 0, 1, 0, 1])
    assert clipped == 4
    np.testing.assert_array_equal(p, original)
    q = 1 - 1e-6
    expected = (
        -math.log(1e-6) - math.log1p(-q) + math.log(2) - math.log1p(-1e-6) - math.log(q)
    ) / 5
    assert loss == pytest.approx(expected, rel=1e-12)
    assert metrics.log_loss([1e-6, 1 - 1e-6], [0, 1])[1] == 0


def test_calibration_table_and_ece():
    p = np.array([0.05, 0.15, 0.15, 0.95, 1.0])
    y = np.array([0, 0, 1, 1, 1])
    table = metrics.calibration_table(p, y, bins=10)
    assert [row["count"] for row in table] == [1, 2, 0, 0, 0, 0, 0, 0, 0, 2]
    assert table[1]["mean_p"] == pytest.approx(0.15) and table[1]["mean_y"] == 0.5
    assert table[2]["mean_p"] is None and table[9]["mean_p"] == pytest.approx(0.975)
    expected = (1 * 0.05 + 2 * 0.35 + 2 * 0.025) / 5
    assert metrics.ece(p, y, bins=10) == pytest.approx(expected)


def grouped_sample(seed=0, n_groups=30):
    rng = np.random.default_rng(seed)
    sizes = rng.integers(1, 5, n_groups)
    groups = np.repeat(np.arange(n_groups), sizes)
    p = rng.uniform(0.05, 0.95, len(groups))
    return p, (rng.random(len(groups)) < p).astype(np.int64), groups


def test_grouped_bootstrap_is_seeded_and_deterministic():
    p, y, groups = grouped_sample()
    first = metrics.grouped_bootstrap("log_loss", p, y, groups, n=300, seed=4)
    assert first == metrics.grouped_bootstrap("log_loss", p, y, groups, n=300, seed=4)
    assert first != metrics.grouped_bootstrap("log_loss", p, y, groups, n=300, seed=5)
    estimate, low, high = first
    assert estimate == metrics.log_loss(p, y)[0] and low < high


def test_grouped_bootstrap_moves_whole_groups():
    _, y, groups = grouped_sample(1)
    n = len(groups)
    p = (np.arange(n) + 0.5) / n  # probabilities encode row ids
    sizes = np.bincount(groups)
    seen = []

    def recorder(q, labels):
        rows = np.rint(q * n - 0.5).astype(int)
        seen.append(rows)
        return float(len(rows))

    metrics.grouped_bootstrap(recorder, p, y, groups, n=50, seed=0)
    for rows in seen[:-1]:  # the last call is the full-sample estimate
        drawn = np.bincount(groups[rows], minlength=len(sizes))
        copies = drawn // sizes
        assert (drawn == copies * sizes).all()  # every group is all-in, k times
        assert copies.sum() == len(sizes)  # as many draws as there are groups
        for g in np.flatnonzero(copies):
            members = np.flatnonzero(groups == g)
            assert (np.bincount(rows, minlength=n)[members] == copies[g]).all()
    np.testing.assert_array_equal(seen[-1], np.arange(n))


def test_paired_bootstrap_uses_identical_resamples():
    p, y, groups = grouped_sample(2)
    assert metrics.paired_bootstrap("brier", p, p, y, groups, n=100, seed=1) == (0.0, 0.0, 0.0)
    worse = np.clip(p + 0.2, 0, 1)
    result = metrics.paired_bootstrap("brier", worse, p, y, groups, n=200, seed=1)
    assert result == metrics.paired_bootstrap("brier", worse, p, y, groups, n=200, seed=1)
    assert result[0] == pytest.approx(metrics.brier(worse, y) - metrics.brier(p, y))
    assert result[1] <= result[2]


def test_metric_inputs_are_validated():
    with pytest.raises(ValueError):
        metrics.brier([1.2], [1])
    with pytest.raises(ValueError):
        metrics.brier([0.5], [0.0])
    with pytest.raises(ValueError):
        metrics.log_loss([], [])
    with pytest.raises(ValueError, match="two groups"):
        metrics.grouped_bootstrap("brier", [0.2, 0.4], [0, 1], [7, 7], seed=0)
    with pytest.raises(ValueError, match="unknown metric"):
        metrics.grouped_bootstrap("auc", [0.2, 0.4], [0, 1], [1, 2], seed=0)
    with pytest.raises(ValueError, match="groups"):
        metrics.grouped_bootstrap("brier", [0.2, 0.4], [0, 1], [1.0, 2.0], seed=0)
