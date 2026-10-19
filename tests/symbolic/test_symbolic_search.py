"""Search budgets, determinism, rejection, deduplication, selection and replay.

Synthetic planted relation only (tests/symbolic/_symbolic.py); small populations keep the
run short.
"""

import hashlib
import inspect
import json

import numpy as np
import pytest
from _symbolic import NAMES, TickClock, planted

from vader_intelligence.provenance import json_text
from vader_intelligence.symbolic import (
    Const,
    Op,
    SearchConfig,
    UnknownOperatorSet,
    Var,
    model_from_dict,
    replay,
    search,
)
from vader_intelligence.symbolic.engine import Archive, Scorer
from vader_intelligence.symbolic.expressions import depth, size
from vader_intelligence.symbolic.genetic import nodes

X0, X1, X2 = Var("x0"), Var("x1"), Var("x2")
SMALL = dict(population=20, generations=3, seed=0)


@pytest.fixture(scope="module")
def folds():
    X_train, y_train = planted(1000, 7)
    X_val, y_val = planted(1000, 8)
    return X_train, y_train, X_val, y_val


@pytest.fixture(scope="module")
def planted_result(folds):
    return search(*folds, NAMES, SearchConfig(population=100, generations=15, seed=0))


def op(name, *args):
    return Op(name, tuple(args))


def scorer(folds, y_val=None):
    X_train, y_train, X_val, default_y_val = folds
    y_val = default_y_val if y_val is None else y_val
    return Scorer(X_train, y_train, X_val, y_val, NAMES, "ops-v1", 0.002)


def variables(expr):
    return {node.name for _, node, _ in nodes(expr) if isinstance(node, Var)}


def sha(values):
    return hashlib.sha256(np.ascontiguousarray(values, dtype="<f8").tobytes()).hexdigest()


def test_search_accepts_no_holdout_argument():
    parameters = list(inspect.signature(search).parameters)
    expected = "X_train y_train X_val y_val feature_names config clock".split()
    assert parameters == expected
    assert inspect.signature(search).parameters["clock"].kind is inspect.Parameter.KEYWORD_ONLY
    assert not any("holdout" in p or "test" in p for p in parameters)


def test_generations_budget_stops_the_run(folds):
    result = search(*folds, NAMES, SearchConfig(**SMALL))
    assert result.stop_reason == "generations" and result.reproducible
    assert result.generations_completed == 3
    assert [h["generation"] for h in result.history] == [0, 1, 2, 3]
    counters = result.to_dict()["counters"]
    invalid = sum(counters["invalid"].values())
    assert counters["candidates_evaluated"] == (
        counters["valid"] + invalid + counters["duplicates"]["semantic"]
    )
    assert result.history[-1]["candidates_evaluated"] == counters["candidates_evaluated"]


def test_candidate_cap_is_hard(folds):
    config = SearchConfig(population=20, generations=50, max_candidates=25, seed=0)
    result = search(*folds, NAMES, config)
    assert result.stop_reason == "max_candidates" and result.reproducible
    assert result.candidates_evaluated == 25
    assert result.generations_completed < 50


def test_time_budget_uses_the_injected_clock(folds):
    clock = TickClock()
    config = SearchConfig(population=20, generations=50, max_seconds=5.0, seed=0)
    result = search(*folds, NAMES, config, clock=clock)
    # Start reads 0; evaluations read 1..4; the read of 5 hits the budget.
    assert result.stop_reason == "max_seconds"
    assert result.candidates_evaluated == 4
    assert result.history == ()
    assert result.to_dict()["reproducible"] is False
    assert result.elapsed_seconds == 6.0
    assert "elapsed_seconds" not in json_text(result.to_dict())


def test_same_seed_is_byte_identical_and_seed_matters(folds):
    first = json_text(search(*folds, NAMES, SearchConfig(**SMALL)).to_dict())
    second = json_text(search(*folds, NAMES, SearchConfig(**SMALL)).to_dict())
    other = json_text(search(*folds, NAMES, SearchConfig(**{**SMALL, "seed": 1})).to_dict())
    assert first == second
    assert first != other


def test_planted_relation_is_recovered_and_beats_a_constant(folds, planted_result):
    _, y_train, _, y_val = folds
    rate = y_train.mean()
    constant = -np.mean(y_val * np.log(rate) + (1 - y_val) * np.log(1 - rate))
    selected = planted_result.selected
    assert planted_result.stop_reason == "generations"
    assert selected.validation_log_loss < constant - 0.15
    assert {"x0", "x1"} <= variables(selected.model.expression)
    record = planted_result.to_dict()
    assert record["validation_metrics_selection_biased"] is True
    assert record["selected"]["model"]["operator_set"] == "ops-v1"


def test_front_and_one_standard_error_selection(planted_result):
    record = planted_result.to_dict()
    front = record["front"]
    complexities = [entry["complexity"] for entry in front]
    losses = [entry["validation_log_loss"] for entry in front]
    assert complexities == sorted(set(complexities))
    assert all(later < earlier for earlier, later in zip(losses, losses[1:]))
    selection = record["selection"]
    assert selection["rule"] == "simplest_within_one_standard_error"
    assert selection["min_validation_log_loss"] == min(losses)
    assert selection["threshold"] == min(losses) + selection["standard_error"]
    eligible = [e for e in front if e["validation_log_loss"] <= selection["threshold"]]
    assert record["selected"] == eligible[0]
    for entry in front:
        expression = model_from_dict(entry["model"]).expression
        assert size(expression) == entry["complexity"] <= 25 and depth(expression) <= 5


def test_replay_equals_in_search_probabilities_exactly(folds, planted_result):
    X_train, _, X_val, y_val = folds
    stored = json.loads(json_text(planted_result.to_dict()))["selected"]
    model = model_from_dict(stored["model"])
    assert sha(replay(stored["model"], X_val)) == stored["probabilities_sha256"]["validation"]
    assert sha(model.predict_proba(X_train)) == stored["probabilities_sha256"]["train"]
    q = np.clip(replay(model, X_val), 1e-6, 1 - 1e-6)
    loss = float(np.mean(-np.log(np.where(y_val == 1, q, 1 - q))))
    assert loss == stored["validation_log_loss"]


def test_platt_is_fit_on_the_training_fold_only(folds):
    expr = op("sub", op("mul", Const(2.0), X0), X1)
    flipped = 1 - folds[3]
    outcomes = []
    for y_val in (None, flipped):
        fold_scorer = scorer(folds, y_val)
        outcomes.append(fold_scorer.score(expr, fold_scorer.training_scores(expr), 1))
    assert (outcomes[0].model.a, outcomes[0].model.b) == (outcomes[1].model.a, outcomes[1].model.b)
    assert outcomes[0].validation_log_loss < 0.6 < outcomes[1].validation_log_loss


@pytest.mark.parametrize(
    "expr",
    [Const(0.5), op("sub", X0, X0), op("mul", Const(1e-10), X0), op("abs", Const(-1.0))],
)
def test_near_constant_expressions_are_rejected(folds, expr):
    fold_scorer = scorer(folds)
    assert fold_scorer.score(expr, fold_scorer.training_scores(expr), 1) == "near_constant"


def test_non_finite_and_unstable_expressions_are_rejected(folds):
    X_train, y_train, X_val, y_val = folds
    with_nan = X_val.copy()
    with_nan[3, 2] = np.nan
    archive = Archive(
        Scorer(X_train, y_train, with_nan, y_val, NAMES, "ops-v1", 0.0), 100, 60.0, TickClock()
    )
    assert archive.evaluate(op("add", X0, X2))[1] == "non_finite"
    # x0 takes the value 2 on a third of the rows: pdiv(1, x0 - 2) takes its protected
    # branch there, and a 1e-6 relative input change leaves it (1/2e-6 = 5e5).
    rng = np.random.Generator(np.random.PCG64(5))
    X = rng.choice([1.0, 2.0, 3.0], size=(600, 3))
    y = (rng.random(600) < np.where(X[:, 0] > 2, 0.8, 0.3)).astype(np.int64)
    fold_scorer = Scorer(X[:300], y[:300], X[300:], y[300:], NAMES, "ops-v1", 0.0)
    expr = op("pdiv", Const(1.0), op("sub", X0, Const(2.0)))
    assert fold_scorer.score(expr, fold_scorer.training_scores(expr), 1) == "unstable"
    assert archive.invalid["non_finite"] == 1 and archive.evaluated == 1


def test_duplicates_are_counted_not_rescored(folds):
    archive = Archive(scorer(folds), 100, 60.0, TickClock())
    _, first = archive.evaluate(op("add", X0, X1))
    assert archive.evaluate(op("add", X1, X0))[1] is first
    assert archive.evaluate(op("add", op("add", X1, X0), Const(0.0)))[1] is first
    assert archive.duplicates == {"canonical": 2, "semantic": 0} and archive.evaluated == 1
    _, doubled = archive.evaluate(op("mul", X0, Const(2.0)))
    assert archive.evaluate(op("add", X0, X0))[1] is doubled
    assert archive.evaluate(op("mul", Const(2.0), X0))[1] is doubled
    assert archive.duplicates == {"canonical": 3, "semantic": 1}
    assert archive.evaluated == 3 and len(archive.valid) == 2


def test_oversized_children_are_discarded_and_counted(folds):
    config = SearchConfig(population=30, generations=4, max_depth=3, max_nodes=5, seed=2)
    result = search(*folds, NAMES, config)
    assert result.oversized_discarded > 0
    for candidate in result.front:
        assert size(candidate.model.expression) <= 5 and depth(candidate.model.expression) <= 3


def test_result_record_shape(planted_result):
    record = planted_result.to_dict()
    assert sorted(record) == sorted(
        [
            "engine",
            "config",
            "config_hash",
            "seed",
            "inputs",
            "budgets",
            "stop_reason",
            "reproducible",
            "generations_completed",
            "counters",
            "history",
            "front",
            "selected",
            "selection",
            "validation_metrics_selection_biased",
        ]
    )
    assert record["config"]["constant_range"] == [-2.0, 2.0]
    assert record["config_hash"] == hashlib.sha256(json_text(record["config"]).encode()).hexdigest()
    assert record["inputs"]["feature_names"] == NAMES
    assert sorted(record["counters"]["invalid"]) == sorted(
        ["non_finite", "near_constant", "platt_failure", "unstable"]
    )
    assert set(record["history"][0]) == {
        "generation",
        "valid",
        "best_penalized_train_loss",
        "median_penalized_train_loss",
        "best_validation_log_loss",
        "candidates_evaluated",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"population": 1},
        {"max_depth": 1},
        {"max_nodes": 2},
        {"generations": -1},
        {"max_candidates": 0},
        {"max_seconds": 0.0},
        {"tournament": 21},
        {"elitism": 20},
        {"crossover_rate": 0.8, "mutation_rate": 0.3},
        {"constant_range": (1.0, -1.0)},
        {"complexity_penalty": float("nan")},
        {"seed": -1},
        {"population": True},
    ],
)
def test_config_validation(overrides):
    with pytest.raises(ValueError):
        SearchConfig(**{"population": 20, **overrides})


def test_unknown_operator_set_is_refused():
    with pytest.raises(UnknownOperatorSet):
        SearchConfig(operator_set="ops-v2")


def test_input_validation(folds):
    X_train, y_train, X_val, y_val = folds
    config = SearchConfig(**SMALL)
    with pytest.raises(ValueError, match="integer"):
        search(X_train, y_train.astype(float), X_val, y_val, NAMES, config)
    with pytest.raises(ValueError, match="0 and 1"):
        search(X_train, y_train * 2, X_val, y_val, NAMES, config)
    with pytest.raises(ValueError, match="both classes"):
        search(X_train, np.zeros_like(y_train), X_val, y_val, NAMES, config)
    with pytest.raises(ValueError):
        search(X_train[:, :2], y_train, X_val, y_val, NAMES, config)
    with pytest.raises(ValueError):
        search(X_train, y_train, X_val, y_val, ["x0", "x0", "x2"], config)
    with pytest.raises(TypeError):
        search(X_train, y_train, X_val, y_val, NAMES, SMALL)


def test_planted_data_helper_is_deterministic():
    assert np.array_equal(planted(10, 1)[0], planted(10, 1)[0])
