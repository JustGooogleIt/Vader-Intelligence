"""Bounded symbolic regression engine (spec: docs/specs/symbolic-search-v1.md).

Pure Python + numpy on in-memory arrays. The engine never receives holdout data; its
validation metrics are selection-biased by construction.
"""

from .expressions import (
    OPERATOR_SETS,
    Const,
    NonFiniteError,
    Op,
    UnknownOperatorSet,
    Var,
    canonical,
    describe,
    evaluate_expression,
)
from .expressions import from_json as expression_from_json
from .expressions import to_json as expression_to_json

__all__ = [
    "OPERATOR_SETS",
    "Const",
    "NonFiniteError",
    "Op",
    "UnknownOperatorSet",
    "Var",
    "canonical",
    "describe",
    "evaluate_expression",
    "expression_from_json",
    "expression_to_json",
]
