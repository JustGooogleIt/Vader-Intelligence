import numpy as np
import pytest

from vader_intelligence.models import logistic, search
from vader_intelligence.models.preprocess import FeatureSpec
from vader_intelligence.provenance import json_text

from ._synthetic import SPECS, StepClock, games

GRID4 = [{"C": c} for c in (0.01, 0.1, 1.0, 10.0)]
GBM_GRID = {
    "learning_rate": [0.1],
    "max_iter": [10, 20],
    "max_leaf_nodes": [5],
    "min_samples_leaf": [10],
    "l2_regularization": [0.0],
}


def run(X=None, grid=GRID4, family="logistic", specs=SPECS, **kwargs):
    data = games()
    X = data[0] if X is None else X
    options = dict(n_folds=3, seed=0, max_configs=10, deadline_seconds=None, clock=None)
    return search.grid_search(family, grid, X, data[1], data[2], data[3], specs, **options | kwargs)


def test_selection_and_result_shape():
    result = run()
    means = [r["mean_log_loss"] for r in result.results]
    assert result.selected == int(np.argmin(means)) and result.stop_reason == "exhausted"
    assert result.selected_config == GRID4[result.selected]
    for r in result.results:
        assert len(r["fold_log_loss"]) == 3 and r["se_log_loss"] > 0 and r["clipped"] == 0
    assert len(result.oof_index) == len(result.oof_raw) == len(result.oof_fold)
    tested = np.sort(np.concatenate([test for _, test in result.folds]))
    np.testing.assert_array_equal(result.oof_index, tested)
    summary = result.summary()
    assert json_text(summary) and summary["configs_tried"] == 4 and summary["grid_size"] == 4


def test_ties_go_to_the_first_config():
    result = run(grid=[{"C": 1.0}, {"C": 1.0}])
    assert result.results[0]["mean_log_loss"] == result.results[1]["mean_log_loss"]
    assert result.selected == 0


def test_max_configs_budget():
    limited = run(max_configs=2)
    assert len(limited.results) == 2 and limited.stop_reason == "max_configs"
    assert limited.grid_size == 4 and limited.summary()["configs_tried"] == 2
    assert run(max_configs=4).stop_reason == "exhausted"


def test_deadline_with_injected_clock():
    # StepClock advances 1 s per reading; the deadline is checked before every fold fit.
    result = run(n_folds=2, deadline_seconds=4.5, clock=StepClock())
    assert result.stop_reason == "deadline" and len(result.results) == 2
    assert result.elapsed_seconds == 6.0
    interrupted = run(n_folds=2, deadline_seconds=3.5, clock=StepClock())
    assert interrupted.stop_reason == "deadline" and len(interrupted.results) == 1
    with pytest.raises(ValueError, match="no configuration completed"):
        run(n_folds=2, deadline_seconds=0.5, clock=StepClock())
    with pytest.raises(ValueError, match="clock"):
        run(deadline_seconds=10.0, clock=None)


def test_out_of_fold_predictions_never_come_from_a_model_that_saw_the_row(monkeypatch):
    """A row-id column (unscaled, passed through) lets the fit and score record rows."""
    calls = []

    def fit(Z, y, **kwargs):
        calls.append({"fit": Z[:, 0].astype(int)})
        return logistic.fit_logistic(Z[:, 1:], y, **kwargs)

    def score(params, Z):
        raw = logistic.logit(params, Z[:, 1:])
        calls[-1].update(scored=Z[:, 0].astype(int), raw=raw)
        return raw

    family = search.Family(fit, score, frozenset({"C"}), frozenset())
    monkeypatch.setitem(search.FAMILIES, "logistic", family)
    X, *_ = games()
    X = np.column_stack([np.arange(len(X)), X])
    specs = [FeatureSpec("row_id", missing="forbid", scale=False), *SPECS]
    result = run(X, grid=GRID4[:3], specs=specs)
    assert len(calls) == 3 * 3
    chosen = calls[3 * result.selected : 3 * result.selected + 3]
    for call, (train, test) in zip(chosen, result.folds):
        np.testing.assert_array_equal(call["fit"], train)
        np.testing.assert_array_equal(call["scored"], test)
    seen = [set(call["fit"]) for call in chosen]
    for row, fold, raw in zip(result.oof_index, result.oof_fold, result.oof_raw):
        assert row not in seen[fold]
        call = chosen[fold]
        assert raw == call["raw"][np.searchsorted(call["scored"], row)]


def test_preprocessing_is_fit_inside_each_fold():
    X, *_ = games()
    base = run(X, grid=[{"C": 1.0}])
    later = base.folds[1][1]  # fold 1's test rows: unseen by fold 0, trained on by fold 2
    changed = X.copy()
    changed[later, 0] += 50.0
    changed[later, 1] = np.nan
    other = run(changed, grid=[{"C": 1.0}])
    fold0, fold2 = base.oof_fold == 0, base.oof_fold == 2
    np.testing.assert_array_equal(base.oof_raw[fold0], other.oof_raw[fold0])
    assert not np.array_equal(base.oof_raw[fold2], other.oof_raw[fold2])


def test_gbm_search_is_deterministic():
    first, second = run(grid=GBM_GRID, family="gbm"), run(grid=GBM_GRID, family="gbm")
    assert json_text(first.summary()) == json_text(second.summary())
    np.testing.assert_array_equal(first.oof_raw, second.oof_raw)
    assert first.results[0]["config"]["max_iter"] == 10  # sorted names, values in given order


def test_invalid_grids_are_refused():
    for grid in [{"C": [1.0], "seed": [1]}, {"max_iter": [10]}, {"C": []}, [], {"C": 1.0}]:
        with pytest.raises(ValueError):
            run(grid=grid)
    with pytest.raises(ValueError, match="searchable family"):
        run(family="svm")
    with pytest.raises(ValueError, match="max_configs"):
        run(max_configs=0)
    with pytest.raises(ValueError, match="dates"):
        X, y, dates, groups = games()
        search.grid_search(
            "logistic",
            GRID4,
            X,
            y,
            dates[:-1],
            groups[:-1],
            SPECS,
            n_folds=3,
            seed=0,
            max_configs=1,
            deadline_seconds=None,
            clock=None,
        )
