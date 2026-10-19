"""Expression trees for operator set ops-v1: evaluation, serialization, canonical form.

Invariants: trees are immutable; every node value is finite and clipped to [-1e6, 1e6]
(a NaN or infinity at any node makes the whole expression invalid, it is never repaired);
constants are finite, |c| <= 1e6 and rounded to 12 significant digits, so the decimal
string written by `to_json` parses back to the identical float.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..provenance import json_text

CLIP = 1e6
PROTECT = 1e-6
CONST_DIGITS = 12
CANONICAL_DIGITS = 6
MAX_DEPTH_LIMIT = 16
MAX_NODES_LIMIT = 255
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e[+-]\d+)?\Z", re.ASCII)
COMMUTATIVE = ("add", "mul", "min", "max")


class NonFiniteError(ValueError):
    """An expression produced NaN or infinity at some node."""


class UnknownOperatorSet(ValueError):
    """A model or configuration names an operator set this engine does not implement."""


def round_sig(value, digits):
    """Round a finite real to `digits` significant digits; -0.0 becomes 0.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError("expected a real number")
    value = float(value)
    if not np.isfinite(value):
        raise ValueError("expected a finite number")
    return float(format(value, f".{digits - 1}e")) + 0.0


def number_text(value):
    """Stable decimal text: 12 significant digits, shortest form, exact float round trip."""
    return format(round_sig(value, CONST_DIGITS), f".{CONST_DIGITS}g")


def parse_number(text):
    if not isinstance(text, str) or len(text) > 32 or not NUMBER.fullmatch(text):
        raise ValueError("expected a decimal number string")
    return round_sig(float(text), CONST_DIGITS)


@dataclass(frozen=True)
class Var:
    name: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("variable name must be a nonempty string")


@dataclass(frozen=True)
class Const:
    value: float

    def __post_init__(self):
        value = round_sig(self.value, CONST_DIGITS)
        if abs(value) > CLIP:
            raise ValueError("constant outside [-1e6, 1e6]")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class Op:
    op: str
    args: tuple

    def __post_init__(self):
        if not isinstance(self.op, str) or not isinstance(self.args, tuple) or not self.args:
            raise ValueError("operator node needs a name and a nonempty argument tuple")
        if not all(isinstance(arg, (Var, Const, Op)) for arg in self.args):
            raise ValueError("operator arguments must be expressions")


@dataclass(frozen=True)
class Operator:
    name: str
    arity: int
    function: Callable
    symbol: str | None = None


def _pdiv(a, b):
    out = a.copy()
    np.divide(a, b, out=out, where=np.abs(b) > PROTECT)
    return out


OPS_V1 = (
    Operator("add", 2, np.add, "+"),
    Operator("sub", 2, np.subtract, "-"),
    Operator("mul", 2, np.multiply, "*"),
    Operator("pdiv", 2, _pdiv),
    Operator("min", 2, np.minimum),
    Operator("max", 2, np.maximum),
    Operator("neg", 1, np.negative),
    Operator("abs", 1, np.abs),
    Operator("plog", 1, lambda x: np.log(np.abs(x) + PROTECT)),
    Operator("psqrt", 1, lambda x: np.sqrt(np.abs(x))),
    Operator("tanh", 1, np.tanh),
    Operator("square", 1, np.square),
)
OPERATOR_SETS = {"ops-v1": OPS_V1}


def operators(operator_set):
    """Operator table (name -> Operator, fixed order) or UnknownOperatorSet."""
    if not isinstance(operator_set, str) or operator_set not in OPERATOR_SETS:
        raise UnknownOperatorSet(f"unknown operator set {operator_set!r}")
    return {op.name: op for op in OPERATOR_SETS[operator_set]}


def size(expr):
    """Complexity: node count."""
    return 1 if not isinstance(expr, Op) else 1 + sum(size(arg) for arg in expr.args)


def depth(expr):
    """Levels: a leaf has depth 1."""
    return 1 if not isinstance(expr, Op) else 1 + max(depth(arg) for arg in expr.args)


def check_names(feature_names):
    if not isinstance(feature_names, (list, tuple)) or not feature_names:
        raise ValueError("feature_names must be a nonempty list of strings")
    names = tuple(feature_names)
    if not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names):
        raise ValueError("feature_names must be unique nonempty strings")
    return names


def validate(expr, operator_set="ops-v1", feature_names=None):
    """Raise ValueError unless operators, arities and variables are known and the tree is
    within the engine-wide limits (MAX_DEPTH_LIMIT levels, MAX_NODES_LIMIT nodes)."""
    table = operators(operator_set)
    if not isinstance(expr, (Var, Const, Op)):
        raise ValueError("expected an expression")
    if depth(expr) > MAX_DEPTH_LIMIT or size(expr) > MAX_NODES_LIMIT:
        raise ValueError("expression exceeds depth or size limit")
    stack = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, Op):
            operator = table.get(node.op)
            if operator is None or operator.arity != len(node.args):
                raise ValueError(f"unknown operator or wrong arity: {node.op!r}")
            stack.extend(node.args)
        elif isinstance(node, Var) and feature_names is not None and node.name not in feature_names:
            raise ValueError(f"expression references missing feature {node.name!r}")
    return expr


@dataclass(frozen=True)
class Features:
    """Clipped float64 columns by name; None marks a column containing NaN or infinity."""

    rows: int
    columns: dict


def features(X, feature_names):
    """Prepare X (rows x len(feature_names)) once; leaves read these clipped columns."""
    names = check_names(feature_names)
    X = np.asarray(X, dtype=np.float64)
    if X.ndim != 2 or X.shape[1] != len(names) or X.shape[0] < 1:
        raise ValueError("X must be a nonempty 2-D array with one column per feature name")
    columns = {}
    for i, name in enumerate(names):
        column = np.ascontiguousarray(X[:, i])
        columns[name] = np.clip(column, -CLIP, CLIP) if np.isfinite(column).all() else None
    return Features(X.shape[0], columns)


def evaluate(expr, data, table):
    """Raw scores for prepared features, or None if any node is non-finite."""
    if isinstance(expr, Var):
        return data.columns[expr.name]
    if isinstance(expr, Const):
        return np.full(data.rows, expr.value)
    args = []
    for arg in expr.args:
        value = evaluate(arg, data, table)
        if value is None:
            return None
        args.append(value)
    with np.errstate(all="ignore"):
        raw = table[expr.op].function(*args)
    if not np.isfinite(raw).all():
        return None
    return np.clip(raw, -CLIP, CLIP)


def evaluate_expression(expr, X, feature_names, operator_set="ops-v1"):
    """Clipped raw scores for X (columns named by feature_names).

    Refuses expressions that reference a name not in feature_names; raises NonFiniteError
    instead of returning NaN or infinity.
    """
    names = check_names(feature_names)
    table = operators(operator_set)
    validate(expr, operator_set, names)
    scores = evaluate(expr, features(X, names), table)
    if scores is None:
        raise NonFiniteError("expression output is not finite")
    return scores


def to_json(expr):
    if isinstance(expr, Var):
        return {"var": expr.name}
    if isinstance(expr, Const):
        return {"const": number_text(expr.value)}
    return {"op": expr.op, "args": [to_json(arg) for arg in expr.args]}


def from_json(data, operator_set="ops-v1", feature_names=None, max_depth=None, max_nodes=None):
    """Parse a prefix-tree JSON value, validating as it descends (bounded recursion)."""
    table = operators(operator_set)
    max_depth = MAX_DEPTH_LIMIT if max_depth is None else max_depth
    max_nodes = MAX_NODES_LIMIT if max_nodes is None else max_nodes
    count = 0

    def parse(node, level):
        nonlocal count
        count += 1
        if level > max_depth or count > max_nodes:
            raise ValueError("expression exceeds depth or size limit")
        if not isinstance(node, dict):
            raise ValueError("expression node must be an object")
        keys = sorted(node)
        if keys == ["var"]:
            name = node["var"]
            if feature_names is not None and name not in feature_names:
                raise ValueError(f"expression references missing feature {name!r}")
            return Var(name)
        if keys == ["const"]:
            return Const(parse_number(node["const"]))
        if keys == ["args", "op"]:
            operator = table.get(node["op"]) if isinstance(node["op"], str) else None
            args = node["args"]
            if operator is None or not isinstance(args, list) or len(args) != operator.arity:
                raise ValueError(f"unknown operator or wrong arity: {node['op']!r}")
            return Op(operator.name, tuple(parse(arg, level + 1) for arg in args))
        raise ValueError("expression node must be var, const or op/args")

    return parse(data, 1)


def _is(expr, value):
    return isinstance(expr, Const) and expr.value == value


def _sort_key(expr):
    return json_text(to_json(expr))


def canonical(expr, digits=CANONICAL_DIGITS):
    """Normal form: commutative operands sorted, `x+0`, `x*1`, `x*0`, `neg(neg(x))` folded.

    With `digits`, constants are first rounded to that many significant digits (the
    deduplication key). With digits=None constants are kept, and for finite inputs the
    result computes the same values as `expr` (zero signs aside, which no ops-v1 operator
    can observe), so it is safe to score in place of `expr`.
    """
    if isinstance(expr, Var):
        return expr
    if isinstance(expr, Const):
        return expr if digits is None else Const(round_sig(expr.value, digits))
    args = tuple(canonical(arg, digits) for arg in expr.args)
    if expr.op == "neg" and isinstance(args[0], Op) and args[0].op == "neg":
        return args[0].args[0]
    if expr.op in COMMUTATIVE:
        args = tuple(sorted(args, key=_sort_key))
    if expr.op == "add":
        if _is(args[0], 0.0) or _is(args[1], 0.0):
            return args[1] if _is(args[0], 0.0) else args[0]
    if expr.op == "mul":
        if _is(args[0], 0.0) or _is(args[1], 0.0):
            return Const(0.0)
        if _is(args[0], 1.0) or _is(args[1], 1.0):
            return args[1] if _is(args[0], 1.0) else args[0]
    return Op(expr.op, args)


def simplify(expr):
    """Output-preserving canonical form (constants untouched)."""
    return canonical(expr, None)


def canonical_key(expr):
    return json_text(to_json(canonical(expr)))


def describe(expr):
    """Readable infix text; protected operators keep their names (pdiv, plog, psqrt)."""
    symbols = {op.name: op.symbol for op in OPS_V1 if op.symbol}

    def text(node, top):
        if isinstance(node, Var):
            return node.name
        if isinstance(node, Const):
            value = number_text(node.value)
            return value if top or node.value >= 0 else f"({value})"
        if node.op in symbols:
            body = f"{text(node.args[0], False)} {symbols[node.op]} {text(node.args[1], False)}"
            return body if top else f"({body})"
        if node.op == "neg":
            body = f"-{text(node.args[0], False)}"
            return body if top else f"({body})"
        return f"{node.op}({', '.join(text(arg, True) for arg in node.args)})"

    return text(expr, True)
