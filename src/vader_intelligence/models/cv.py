"""Chronological expanding-window folds with a day embargo and whole-group assignment."""

import numpy as np

from ._common import as_groups


def chronological_folds(dates, groups, *, n_folds, min_train_fraction=0.4, embargo_days=1):
    """Return [(train_index, test_index), ...] as ascending int64 arrays.

    Groups are ordered by (first date, last date, label). Rows of the first
    `min_train_fraction` of that order are never tested; the remaining groups form
    `n_folds` consecutive test blocks of roughly equal row counts. Fold k trains on
    every group whose last date is strictly before, and at least `embargo_days` before,
    the first date in its test block, so train rows strictly precede test rows and no
    group is split. Deterministic; raises ValueError if any fold would be empty.
    """
    dates = _as_dates(dates)
    groups = as_groups(groups, len(dates))
    if type(n_folds) is not int or n_folds < 1:
        raise ValueError("n_folds must be a positive int")
    if not 0 < min_train_fraction < 1:
        raise ValueError("min_train_fraction must be in (0, 1)")
    if type(embargo_days) is not int or embargo_days < 0:
        raise ValueError("embargo_days must be a nonnegative int")
    _, codes = np.unique(groups, return_inverse=True)
    size = np.bincount(codes)
    by_group = dates[np.argsort(codes, kind="stable")]
    starts = np.cumsum(size) - size
    first, last = np.minimum.reduceat(by_group, starts), np.maximum.reduceat(by_group, starts)
    order = np.lexsort((np.arange(len(size)), last, first))
    rows_before = np.cumsum(size[order]) - size[order]
    candidates = order[rows_before >= min_train_fraction * len(dates)]
    if len(candidates) == 0:
        raise ValueError("no rows remain for testing after the minimum training fraction")
    start = np.cumsum(size[candidates]) - size[candidates]
    block = (start * n_folds) // size[candidates].sum()
    embargo = np.timedelta64(embargo_days, "D")
    folds = []
    for k in range(n_folds):
        test_groups = candidates[block == k]
        if len(test_groups) == 0:
            raise ValueError(f"fold {k} would have no test rows")
        test_mask = np.isin(codes, test_groups)
        test_start = dates[test_mask].min()
        eligible = (last < test_start) & (last + embargo <= test_start)
        eligible[test_groups] = False
        train_mask = eligible[codes]
        if not train_mask.any():
            raise ValueError(f"fold {k} would have no training rows")
        train, test = np.flatnonzero(train_mask), np.flatnonzero(test_mask)
        _check_fold(k, dates, codes, train, test, embargo)
        folds.append((train, test))
    return folds


def _as_dates(dates):
    dates = np.asarray(dates)
    if dates.dtype.kind != "M":
        try:
            dates = dates.astype("datetime64")
        except (TypeError, ValueError) as exc:
            raise ValueError("dates must be datetime64-convertible") from exc
    if dates.ndim != 1 or len(dates) == 0:
        raise ValueError("dates must be a nonempty 1-D array")
    if np.isnat(dates).any():
        raise ValueError("dates contain NaT")
    return dates


def _check_fold(k, dates, codes, train, test, embargo):
    """Invariants re-checked on the output, independently of how it was built."""
    if np.intersect1d(codes[train], codes[test]).size:
        raise AssertionError(f"fold {k}: a group appears in both train and test")
    latest, earliest = dates[train].max(), dates[test].min()
    if not (latest < earliest and latest + embargo <= earliest):
        raise AssertionError(f"fold {k}: training rows do not precede test rows by the embargo")
