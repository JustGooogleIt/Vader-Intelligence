"""Bounded grid search on chronological folds, with out-of-fold predictions.

Inside every fold the preprocessor and the model are fit on that fold's training rows
only and score only that fold's test rows. Test blocks are disjoint, so each
out-of-fold prediction comes from exactly one model that never saw its row.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product
from typing import Callable

import numpy as np

from . import gbm, logistic
from ._common import as_labels, as_matrix, as_seed, sigmoid
from .cv import chronological_folds
from .metrics import log_loss
from .preprocess import Preprocessor


@dataclass(frozen=True)
class Family:
    fit: Callable
    score: Callable
    required: frozenset
    optional: frozenset


FAMILIES = {
    "logistic": Family(
        logistic.fit_logistic, logistic.logit, frozenset({"C"}), frozenset({"max_iter", "tol"})
    ),
    "gbm": Family(
        gbm.fit_gbm,
        gbm.raw_score,
        frozenset(
            {"learning_rate", "max_iter", "max_leaf_nodes", "min_samples_leaf", "l2_regularization"}
        ),
        frozenset({"max_bins"}),
    ),
}


@dataclass(frozen=True, eq=False)  # holds arrays: no elementwise ==
class SearchResult:
    """`results[i]` = {"config", "fold_log_loss", "mean_log_loss", "se_log_loss",
    "clipped"} for each completed config, in grid order; `selected` indexes it.
    `oof_raw[j]` is the selected config's raw log-odds for row `oof_index[j]`, produced
    by the model of fold `oof_fold[j]`. `stop_reason` is "exhausted", "max_configs" or
    "deadline"; a config interrupted by the deadline is discarded."""

    family: str
    seed: int
    results: tuple
    selected: int
    oof_index: np.ndarray
    oof_raw: np.ndarray
    oof_fold: np.ndarray
    folds: tuple
    grid_size: int
    max_configs: int
    deadline_seconds: float | None
    stop_reason: str
    elapsed_seconds: float | None

    @property
    def selected_config(self):
        return dict(self.results[self.selected]["config"])

    def summary(self):
        """JSON-ready and deterministic (no arrays, no elapsed time) for `training`."""
        return {
            "family": self.family,
            "seed": self.seed,
            "fold_sizes": [[len(train), len(test)] for train, test in self.folds],
            "grid_size": self.grid_size,
            "max_configs": self.max_configs,
            "deadline_seconds": self.deadline_seconds,
            "configs_tried": len(self.results),
            "stop_reason": self.stop_reason,
            "selected": self.selected,
            "selected_config": self.selected_config,
            "results": [dict(result) for result in self.results],
            "oof_rows": len(self.oof_index),
        }


def expand_grid(grid):
    """A mapping name -> values expands to the product in sorted-name order with values
    in the given order; a sequence of mappings is used as given. Order defines
    simplicity: on a tie in mean log loss the earlier config wins."""
    if isinstance(grid, Mapping):
        names = sorted(grid)
        for name in names:
            if not isinstance(grid[name], (list, tuple)) or not grid[name]:
                raise ValueError(f"grid values for {name!r} must be a nonempty list")
        configs = [dict(zip(names, values)) for values in product(*(grid[n] for n in names))]
    else:
        configs = [dict(config) for config in grid]
    if not configs:
        raise ValueError("grid is empty")
    return configs


def fit_model(family, config, X, y, specs, *, seed):
    """Fit preprocessing, then the model, on exactly these rows: (preprocessor, params)."""
    model = _family(family, config)
    X = as_matrix(X, allow_nan=True)
    preprocessor = Preprocessor.fit(X, specs)
    return preprocessor, model.fit(preprocessor.transform(X), y, **config, seed=seed)


def raw_scores(family, preprocessor, params, X):
    """Raw log-odds of fitted (preprocessor, params) for rows X."""
    return _family(family).score(params, preprocessor.transform(X))


def grid_search(
    family,
    grid,
    X,
    y,
    dates,
    groups,
    specs,
    *,
    n_folds,
    seed,
    max_configs,
    deadline_seconds,
    clock,
    min_train_fraction=0.4,
    embargo_days=1,
):
    """Evaluate configs in grid order on `chronological_folds`; select the lowest mean
    fold log loss (first wins ties). Stops after `max_configs` configs or once
    `clock() - start >= deadline_seconds` (checked before every fold fit; `clock` is an
    injected monotonic clock, unused when `deadline_seconds` is None)."""
    configs = expand_grid(grid)
    for config in configs:
        _family(family, config)
    X = as_matrix(X, allow_nan=True)
    y = as_labels(y, len(X))
    seed = as_seed(seed)
    specs = tuple(specs)
    if np.shape(dates) != (len(X),):
        raise ValueError(f"dates must be 1-D with {len(X)} entries")
    if type(max_configs) is not int or max_configs < 1:
        raise ValueError("max_configs must be a positive int")
    if deadline_seconds is not None and (
        isinstance(deadline_seconds, bool) or not deadline_seconds > 0 or not callable(clock)
    ):
        raise ValueError("deadline_seconds must be positive and clock a callable")
    folds = chronological_folds(
        dates,
        groups,
        n_folds=n_folds,
        min_train_fraction=min_train_fraction,
        embargo_days=embargo_days,
    )
    for train, test in folds:
        if np.intersect1d(train, test).size:
            raise AssertionError("a fold trains on one of its test rows")
    start = clock() if deadline_seconds is not None else None
    results, stop_reason = [], "exhausted"
    selected, selected_oof, best_loss = None, None, np.inf
    for index, config in enumerate(configs):
        if index == max_configs:
            stop_reason = "max_configs"
            break
        oof = np.full(len(X), np.nan)
        losses, clipped = [], 0
        for train, test in folds:
            if deadline_seconds is not None and clock() - start >= deadline_seconds:
                stop_reason = "deadline"
                break
            preprocessor, params = fit_model(family, config, X[train], y[train], specs, seed=seed)
            raw = raw_scores(family, preprocessor, params, X[test])
            if not np.isnan(oof[test]).all():
                raise AssertionError("a row received two out-of-fold predictions")
            oof[test] = raw
            loss, n_clipped = log_loss(sigmoid(raw), y[test])
            losses.append(loss)
            clipped += n_clipped
        if stop_reason == "deadline":
            break
        se = float(np.std(losses, ddof=1) / np.sqrt(len(losses))) if len(losses) > 1 else None
        results.append(
            {
                "config": dict(config),
                "fold_log_loss": losses,
                "mean_log_loss": float(np.mean(losses)),
                "se_log_loss": se,
                "clipped": clipped,
            }
        )
        if results[-1]["mean_log_loss"] < best_loss:  # strict: the first of equals wins
            selected, selected_oof = len(results) - 1, oof
            best_loss = results[-1]["mean_log_loss"]
    if not results:
        raise ValueError(f"no configuration completed within budget ({stop_reason})")
    fold_of = np.full(len(X), -1)
    for k, (_, test) in enumerate(folds):
        fold_of[test] = k
    oof_index = np.flatnonzero(fold_of >= 0)
    return SearchResult(
        family=family,
        seed=seed,
        results=tuple(results),
        selected=selected,
        oof_index=oof_index,
        oof_raw=selected_oof[oof_index],
        oof_fold=fold_of[oof_index],
        folds=tuple(folds),
        grid_size=len(configs),
        max_configs=max_configs,
        deadline_seconds=deadline_seconds,
        stop_reason=stop_reason,
        elapsed_seconds=None if start is None else float(clock() - start),
    )


def _family(family, config=None):
    if family not in FAMILIES:
        raise ValueError(f"unknown searchable family {family!r}; expected {sorted(FAMILIES)}")
    model = FAMILIES[family]
    if config is not None:
        keys = set(config)
        missing, unknown = model.required - keys, keys - model.required - model.optional
        if missing or unknown:
            raise ValueError(
                f"{family} config: missing {sorted(missing)}, unknown {sorted(unknown)}"
            )
    return model
