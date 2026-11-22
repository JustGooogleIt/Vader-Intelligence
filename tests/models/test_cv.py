import datetime

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vader_intelligence.models.cv import chronological_folds

from ._synthetic import games

DAY = np.timedelta64(1, "D")


def check_invariants(folds, dates, groups, embargo_days):
    previous_train = np.array([], dtype=np.int64)
    tested = []
    for train, test in folds:
        assert train.dtype == np.int64 and (np.diff(train) > 0).all() and (np.diff(test) > 0).all()
        assert not set(groups[train]) & set(groups[test])  # no group on both sides
        assert dates[train].max() < dates[test].min()  # chronological
        assert dates[train].max() + embargo_days * DAY <= dates[test].min()  # embargo
        assert set(previous_train) <= set(train)  # expanding window
        previous_train = train
        tested.append(test)
    joined = np.concatenate(tested)
    assert len(joined) == len(set(joined))  # test blocks are disjoint


def test_game_folds_respect_groups_embargo_and_order():
    X, y, dates, groups = games()
    for embargo in (0, 1, 3):
        folds = chronological_folds(dates, groups, n_folds=4, embargo_days=embargo)
        assert len(folds) == 4
        check_invariants(folds, dates, groups, embargo)
        assert len(np.concatenate([t for _, t in folds])) <= 0.6 * len(dates)


def test_folds_are_deterministic_and_independent_of_row_order():
    _, _, dates, groups = games(seed=3)
    folds = chronological_folds(dates, groups, n_folds=3)
    again = chronological_folds(dates, groups, n_folds=3)
    assert all((a == c).all() and (b == d).all() for (a, b), (c, d) in zip(folds, again))
    perm = np.random.default_rng(0).permutation(len(dates))
    shuffled = chronological_folds(dates[perm], groups[perm], n_folds=3)
    for (train, test), (s_train, s_test) in zip(folds, shuffled):
        assert sorted(perm[s_train]) == list(train) and sorted(perm[s_test]) == list(test)


def test_group_spanning_the_cutoff_is_in_neither_train_nor_test():
    days = ["05-01", "05-01", "05-02", "05-02", "05-03", "05-03", "05-04", "05-04"]
    dates = np.array([f"2026-{d}" for d in days], "datetime64[D]")
    groups = np.array(["long", "a", "a", "b", "c", "c", "long", "d"])
    folds = chronological_folds(dates, groups, n_folds=1, min_train_fraction=0.6, embargo_days=0)
    check_invariants(folds, dates, groups, 0)
    train, test = folds[0]
    assert set(groups[train]) == {"a", "b"} and set(groups[test]) == {"c", "d"}


def test_accepts_date_objects_and_timestamps():
    days = [datetime.date(2026, 5, 1) + datetime.timedelta(days=i // 2) for i in range(20)]
    folds = chronological_folds(days, np.arange(20) // 2, n_folds=2)
    check_invariants(folds, np.asarray(days, dtype="datetime64[D]"), np.arange(20) // 2, 1)
    stamps = np.datetime64("2026-05-01T12:00") + np.arange(20) * np.timedelta64(7, "h")
    folds = chronological_folds(stamps, np.arange(20), n_folds=2, embargo_days=1)
    check_invariants(folds, stamps, np.arange(20), 1)


def test_refusals():
    dates = np.datetime64("2026-05-01") + np.arange(3) * DAY
    with pytest.raises(ValueError, match="fold"):
        chronological_folds(dates, np.arange(3), n_folds=5)  # too few rows
    same_day = np.repeat(np.datetime64("2026-05-01"), 10)
    with pytest.raises(ValueError, match="no training rows"):
        chronological_folds(same_day, np.arange(10), n_folds=2)
    week = np.datetime64("2026-05-01") + np.arange(10) * DAY
    with pytest.raises(ValueError, match="no training rows"):
        chronological_folds(week, np.arange(10), n_folds=2, embargo_days=30)
    with pytest.raises(ValueError, match="no test rows|no rows remain"):
        chronological_folds(week, np.zeros(10, dtype=int), n_folds=1)  # one group only
    for kwargs in [
        dict(n_folds=0),
        dict(n_folds=2, min_train_fraction=1.0),
        dict(n_folds=2, embargo_days=-1),
        dict(n_folds=2, embargo_days=1.5),
    ]:
        with pytest.raises(ValueError):
            chronological_folds(week, np.arange(10), **kwargs)
    with pytest.raises(ValueError, match="NaT"):
        chronological_folds(np.array(["NaT"] * 10, "datetime64[D]"), np.arange(10), n_folds=2)
    with pytest.raises(ValueError, match="groups"):
        chronological_folds(week, np.arange(9), n_folds=2)


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 40), st.integers(0, 12)), min_size=2, max_size=80),
    st.integers(1, 4),
    st.integers(0, 3),
    st.sampled_from([0.2, 0.4, 0.6]),
)
def test_invariants_hold_or_fold_is_refused(rows, n_folds, embargo, fraction):
    dates = np.datetime64("2026-04-01") + np.array([d for d, _ in rows]) * DAY
    groups = np.array([g for _, g in rows])
    try:
        folds = chronological_folds(
            dates, groups, n_folds=n_folds, min_train_fraction=fraction, embargo_days=embargo
        )
    except ValueError as exc:
        assert "fold" in str(exc) or "no rows remain" in str(exc)
        return
    assert len(folds) == n_folds
    check_invariants(folds, dates, groups, embargo)
