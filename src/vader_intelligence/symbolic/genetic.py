"""Random trees and variation operators, driven only by an explicit numpy Generator.

These functions do not enforce depth/size limits on their results: the search loop checks
every child and discards (and counts) oversized ones.
"""

import numpy as np

from .expressions import CLIP, Const, Op, Var

MUTATIONS = ("point", "subtree", "hoist")


def nodes(expr, path=(), level=1):
    """(path, node, level) for every node in prefix order; the root has level 1."""
    found = [(path, expr, level)]
    if isinstance(expr, Op):
        for i, arg in enumerate(expr.args):
            found.extend(nodes(arg, path + (i,), level + 1))
    return found


def replace(expr, path, new):
    if not path:
        return new
    args = list(expr.args)
    args[path[0]] = replace(args[path[0]], path[1:], new)
    return Op(expr.op, tuple(args))


def random_terminal(rng, names, constant_range):
    i = int(rng.integers(len(names) + 1))
    if i < len(names):
        return Var(names[i])
    return Const(rng.uniform(*constant_range))


def random_tree(rng, ops, names, constant_range, depth, full, root_operator=False):
    """Full: operators down to `depth` levels. Grow: each node draws from operators and
    terminals together, so branches may stop early (the root too, unless root_operator)."""
    if depth <= 1:
        return random_terminal(rng, names, constant_range)
    if full or root_operator:
        operator = ops[int(rng.integers(len(ops)))]
    else:
        i = int(rng.integers(len(ops) + len(names) + 1))
        if i >= len(ops):
            return random_terminal(rng, names, constant_range)
        operator = ops[i]
    args = tuple(
        random_tree(rng, ops, names, constant_range, depth - 1, full) for _ in range(operator.arity)
    )
    return Op(operator.name, args)


def crossover(rng, first, second):
    """Replace a uniformly chosen subtree of `first` by one of `second`."""
    target = nodes(first)
    donor = nodes(second)
    path = target[int(rng.integers(len(target)))][0]
    return replace(first, path, donor[int(rng.integers(len(donor)))][1])


def mutate(rng, expr, kind, ops, names, constant_range, max_depth):
    """Point: same-arity operator, perturbed constant or new terminal. Subtree: a grown
    tree that fits the remaining depth. Hoist: a subtree replaced by one of its own
    subtrees."""
    points = nodes(expr)
    path, node, level = points[int(rng.integers(len(points)))]
    if kind == "point":
        if isinstance(node, Op):
            same = [op for op in ops if op.arity == len(node.args) and op.name != node.op]
            if not same:
                return expr
            new = Op(same[int(rng.integers(len(same)))].name, node.args)
        elif isinstance(node, Const):
            width = constant_range[1] - constant_range[0]
            new = Const(float(np.clip(node.value + rng.normal(0.0, 0.1 * width), -CLIP, CLIP)))
        else:
            new = random_terminal(rng, names, constant_range)
    elif kind == "subtree":
        levels = max(1, max_depth - level + 1)
        new = random_tree(rng, ops, names, constant_range, int(rng.integers(1, levels + 1)), False)
    elif kind == "hoist":
        inner = nodes(node)
        new = inner[int(rng.integers(len(inner)))][1]
    else:
        raise ValueError(f"unknown mutation {kind!r}")
    return replace(expr, path, new)
