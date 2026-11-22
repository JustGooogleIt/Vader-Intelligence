"""Platt fit, natural-log loss with counted (never applied) clipping, and stored models.

Synthetic data only.
"""

import json
import math
import warnings

import numpy as np
import pytest

from vader_intelligence.provenance import json_text
from vader_intelligence.symbolic import (
    Model,
    Op,
    UnknownOperatorSet,
    Var,
    expression_to_json,
    model_from_dict,
    model_to_dict,
    replay,
)
from vader_intelligence.symbolic.calibration import (
    log_loss_rows,
    platt_fit,
    probabilities,
    sigmoid,
)

X0, X1 = Var("x0"), Var("x1")


def op(name, *args):
    return Op(name, tuple(args))


def test_platt_recovers_known_coefficients():
    rng = np.random.Generator(np.random.PCG64(11))
    scores = rng.normal(0.0, 3.0, size=20000) + 5.0
    y = (rng.random(scores.size) < sigmoid(0.8 * scores - 4.3)).astype(np.int64)
    a, b = platt_fit(scores, y)
    assert abs(a - 0.8) < 0.05 and abs(b + 4.3) < 0.25
    assert a == float(format(a, ".11e"))  # stored precision: 12 significant digits


def test_platt_is_bounded_on_separable_data_and_fails_on_zero_spread():
    scores = np.array([-2.0, -1.0, 1.0, 2.0])
    a, b = platt_fit(scores, np.array([0, 0, 1, 1]))
    assert math.isfinite(a) and math.isfinite(b) and a > 0
    assert platt_fit(np.full(4, 3.0), np.array([0, 1, 0, 1])) is None


def test_log_loss_clips_for_loss_only_and_counts_clips():
    p = np.array([0.0, 1.0, 0.5, 1e-7, 0.25])
    y = np.array([0, 1, 1, 0, 0])
    original = p.copy()
    rows, clipped = log_loss_rows(p, y)
    assert clipped == 3
    assert np.array_equal(p, original)
    assert rows[2] == math.log(2) and rows[4] == -math.log(0.75)
    assert rows[0] == -math.log(1 - 1e-6)


def test_sigmoid_is_stable_at_extremes():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        p = probabilities(np.array([-1e6, 0.0, 1e6]), 1e3, 0.0)
    assert p[0] == 0.0 and p[2] == 1.0 and abs(p[1] - 0.5) < 1e-15


def test_model_round_trip_and_operator_set_version_refusal():
    model = Model(op("sub", X0, op("tanh", X1)), 1.5, -0.25, ("x0", "x1"))
    stored = json.loads(json_text(model_to_dict(model)))
    assert stored == {
        "expression": expression_to_json(model.expression),
        "a": "1.5",
        "b": "-0.25",
        "operator_set": "ops-v1",
        "feature_names": ["x0", "x1"],
    }
    assert model_from_dict(stored) == model
    X = np.array([[0.5, -1.0], [2.0, 3.0]])
    assert np.array_equal(replay(stored, X), model.predict_proba(X))
    with pytest.raises(UnknownOperatorSet, match="ops-v2"):
        model_from_dict({**stored, "operator_set": "ops-v2"})
    with pytest.raises(UnknownOperatorSet):
        Model(X0, 1.0, 0.0, ("x0",), "ops-v0")
    with pytest.raises(ValueError):
        model_from_dict({**stored, "extra": True})
    with pytest.raises(ValueError, match="missing feature"):
        model_from_dict({**stored, "feature_names": ["x0"]})
