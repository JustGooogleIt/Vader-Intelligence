"""Operators at their boundaries, serialization and canonical form. Synthetic inputs."""

import json
import math

import numpy as np
import pytest

from vader_intelligence.provenance import json_text
from vader_intelligence.symbolic import (
    Const,
    NonFiniteError,
    Op,
    Var,
    canonical,
    describe,
    evaluate_expression,
    expression_from_json,
    expression_to_json,
)
from vader_intelligence.symbolic.expressions import (
    OPS_V1,
    canonical_key,
    evaluate,
    features,
    operators,
    simplify,
    size,
)
from vader_intelligence.symbolic.genetic import random_tree

X0, X1 = Var("x0"), Var("x1")


def op(name, *args):
    return Op(name, tuple(args))


def run(expr, *columns):
    X = np.column_stack([np.asarray(c, dtype=float) for c in columns])
    return evaluate_expression(expr, X, [f"x{i}" for i in range(len(columns))])


def test_protected_division_near_zero():
    b = [0.0, 1e-7, 1e-6, -1e-6, 4e-6, -2.0]
    out = run(op("pdiv", X0, X1), [3.0] * 6, b)
    assert out.tolist() == [3.0, 3.0, 3.0, 3.0, 3.0 / 4e-6, -1.5]


def test_protected_log_and_sqrt_at_zero_and_negative():
    x = [0.0, -1.0, -1e-12, 4.0]
    assert np.array_equal(run(op("plog", X0), x), np.log(np.abs(x) + 1e-6))
    assert run(op("plog", X0), [0.0])[0] == math.log(1e-6)
    assert run(op("psqrt", X0), [-4.0, 0.0, 9.0]).tolist() == [2.0, 0.0, 3.0]


def test_every_node_is_clipped_to_one_million():
    assert run(op("square", X0), [1e4, -1e4]).tolist() == [1e6, 1e6]
    assert run(X0, [-1e9, 5.0]).tolist() == [-1e6, 5.0]
    assert run(op("mul", X0, X0), [1e6]).tolist() == [1e6]
    assert run(op("pdiv", X0, X1), [1e3], [2e-6]).tolist() == [1e6]
    # Clipping happens at each node, not only at the root.
    assert run(op("sub", op("square", X0), X1), [1e4], [1.0]).tolist() == [1e6 - 1.0]


def test_nan_or_infinity_anywhere_makes_the_expression_invalid():
    # Protection would mask a NaN denominator; the node check must not.
    with pytest.raises(NonFiniteError):
        run(op("pdiv", X0, X1), [1.0, 2.0], [np.nan, 1.0])
    with pytest.raises(NonFiniteError):
        run(op("tanh", X0), [np.inf])
    assert run(X0, [1.0, 2.0], [np.nan, np.nan]).tolist() == [1.0, 2.0]


def test_ops_v1_table_is_exactly_the_specified_set():
    assert [(o.name, o.arity) for o in OPS_V1] == [
        ("add", 2),
        ("sub", 2),
        ("mul", 2),
        ("pdiv", 2),
        ("min", 2),
        ("max", 2),
        ("neg", 1),
        ("abs", 1),
        ("plog", 1),
        ("psqrt", 1),
        ("tanh", 1),
        ("square", 1),
    ]


def test_evaluate_refuses_missing_feature_names():
    with pytest.raises(ValueError, match="missing feature 'x9'"):
        evaluate_expression(op("add", X0, Var("x9")), np.zeros((2, 2)), ["x0", "x1"])
    with pytest.raises(ValueError):
        evaluate_expression(X0, np.zeros((2, 2)), ["x0"])


def test_constants_are_twelve_digit_decimal_strings():
    assert Const(0.1234567890123456).value == 0.123456789012
    assert Const(-0.0).value == 0.0
    for value, text in [(0.25, "0.25"), (1e6, "1000000"), (1e-7, "1e-07"), (-1.5, "-1.5")]:
        assert expression_to_json(Const(value)) == {"const": text}
    with pytest.raises(ValueError):
        Const(2e6)
    with pytest.raises(ValueError):
        Const(float("nan"))


def test_expression_json_round_trip_is_exact():
    expr = op(
        "add",
        op("mul", Const(1 / 3), X0),
        op("pdiv", op("plog", X1), Const(-2.718281828459045e-5)),
    )
    data = json.loads(json_text(expression_to_json(expr)))
    assert data["args"][0]["args"][0] == {"const": "0.333333333333"}
    back = expression_from_json(data, feature_names=["x0", "x1"])
    assert back == expr
    assert json_text(expression_to_json(back)) == json_text(data)


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "div", "args": [{"var": "x0"}, {"var": "x1"}]},
        {"op": "add", "args": [{"var": "x0"}]},
        {"op": "neg", "args": [{"var": "x0"}], "extra": 1},
        {"const": 0.5},
        {"const": "1E5"},
        {"const": "nan"},
        {"var": "x7"},
        [],
    ],
)
def test_expression_json_rejects_malformed_nodes(bad):
    with pytest.raises(ValueError):
        expression_from_json(bad, feature_names=["x0", "x1"])


def test_expression_json_enforces_depth_and_size_limits():
    deep = X0
    for _ in range(6):
        deep = op("neg", deep)
    data = expression_to_json(deep)
    assert expression_from_json(data, max_depth=7) == deep
    with pytest.raises(ValueError, match="limit"):
        expression_from_json(data, max_depth=6)
    with pytest.raises(ValueError, match="limit"):
        expression_from_json(data, max_nodes=6)


def test_canonical_form_folds_the_specified_identities():
    zero, one = Const(0.0), Const(1.0)
    assert canonical(op("add", X0, zero)) == X0
    assert canonical(op("add", zero, X0)) == X0
    assert canonical(op("mul", X0, one)) == X0
    assert canonical(op("mul", one, X0)) == X0
    assert canonical(op("mul", X0, zero)) == zero
    assert canonical(op("neg", op("neg", X0))) == X0
    assert canonical(op("neg", op("neg", op("neg", X0)))) == op("neg", X0)
    assert canonical(op("add", X0, op("mul", X1, zero))) == X0
    # Commutative operands are sorted; non-commutative ones are not.
    for name in ("add", "mul", "min", "max"):
        assert canonical(op(name, X1, X0)) == canonical(op(name, X0, X1))
    assert canonical(op("sub", X1, X0)) != canonical(op("sub", X0, X1))
    # Constants are rounded to 6 significant digits for the key only.
    assert canonical_key(op("mul", X0, Const(0.1234561))) == canonical_key(
        op("mul", Const(0.1234564), X0)
    )
    assert canonical_key(Const(0.123457)) != canonical_key(Const(0.123458))
    assert simplify(op("mul", X0, Const(0.1234561))).args[0] == Const(0.1234561)


def test_simplify_computes_identical_outputs():
    rng = np.random.Generator(np.random.PCG64(3))
    ops = tuple(operators("ops-v1").values())
    data = features(rng.normal(size=(50, 2)), ["x0", "x1"])
    table = operators("ops-v1")
    for i in range(300):
        tree = random_tree(rng, ops, ("x0", "x1"), (-2.0, 2.0), 2 + i % 4, i % 2 == 0)
        before, after = evaluate(tree, data, table), evaluate(simplify(tree), data, table)
        assert size(simplify(tree)) <= size(tree)
        assert before is not None and np.array_equal(before, after)


def test_describe_is_readable_infix():
    expr = op("sub", op("mul", Const(2.0), X0), op("pdiv", X1, Const(-0.5)))
    assert describe(expr) == "(2 * x0) - pdiv(x1, -0.5)"
    assert describe(op("add", X0, Const(-1.0))) == "x0 + (-1)"
    assert describe(op("neg", op("plog", X0))) == "-plog(x0)"
    assert describe(op("max", op("neg", X0), X1)) == "max(-x0, x1)"
