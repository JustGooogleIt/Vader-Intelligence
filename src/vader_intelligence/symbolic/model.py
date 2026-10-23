"""Stored symbolic model {expression, a, b, operator_set, feature_names} and its replay.

`predict_proba` uses the same evaluation and probability code as the search, so replaying
a stored model on the same inputs reproduces the in-search probabilities bit for bit.
"""

from dataclasses import dataclass

from .calibration import probabilities
from .expressions import (
    CONST_DIGITS,
    check_names,
    evaluate_expression,
    from_json,
    number_text,
    operators,
    parse_number,
    round_sig,
    to_json,
    validate,
)

MODEL_KEYS = ["a", "b", "expression", "feature_names", "operator_set"]


@dataclass(frozen=True)
class Model:
    expression: object
    a: float
    b: float
    feature_names: tuple
    operator_set: str = "ops-v1"

    def __post_init__(self):
        operators(self.operator_set)
        names = check_names(self.feature_names)
        validate(self.expression, self.operator_set, names)
        object.__setattr__(self, "feature_names", names)
        object.__setattr__(self, "a", round_sig(self.a, CONST_DIGITS))
        object.__setattr__(self, "b", round_sig(self.b, CONST_DIGITS))

    def predict_proba(self, X):
        """P(y=1) for X whose columns are exactly `feature_names`, in order."""
        scores = evaluate_expression(self.expression, X, self.feature_names, self.operator_set)
        return probabilities(scores, self.a, self.b)


def model_to_dict(model):
    return {
        "expression": to_json(model.expression),
        "a": number_text(model.a),
        "b": number_text(model.b),
        "operator_set": model.operator_set,
        "feature_names": list(model.feature_names),
    }


def model_from_dict(data):
    """Inverse of model_to_dict; an unknown operator_set raises UnknownOperatorSet."""
    if not isinstance(data, dict) or sorted(data) != MODEL_KEYS:
        raise ValueError(f"model must have exactly the keys {MODEL_KEYS}")
    operators(data["operator_set"])
    names = check_names(data["feature_names"])
    expression = from_json(data["expression"], data["operator_set"], names)
    return Model(
        expression, parse_number(data["a"]), parse_number(data["b"]), names, data["operator_set"]
    )


def replay(model, X):
    """Recompute probabilities from a Model or its stored dict."""
    if isinstance(model, dict):
        model = model_from_dict(model)
    if not isinstance(model, Model):
        raise TypeError("replay expects a Model or a stored model dict")
    return model.predict_proba(X)
