"""The `symbolic` family for model artifacts: raw log-odds a·expr(Z) + b (roadmap C).

The artifact's preprocessor produces Z; the stored engine model evaluates its expression
on Z's columns (which must be exactly the model's feature names) with the same code the
search used, so artifact probabilities equal the engine's replay. Registered on import;
artifact construction scores zero rows, so malformed params are refused before an
artifact exists.
"""

import numpy as np

from ..models.artifact import ArtifactError, register_family
from .expressions import evaluate_expression
from .model import model_from_dict

FAMILY = "symbolic"


def raw_score(params, Z, names):
    """Raw log-odds per row of Z. On zero rows (artifact construction and verification)
    it only validates: params are exactly {"model"}, the stored model parses and its
    feature names are the preprocessor outputs, in order."""
    if set(params) != {"model"}:
        raise ArtifactError("symbolic params must be exactly {'model'}")
    model = model_from_dict(params["model"])
    if list(model.feature_names) != list(names):
        raise ArtifactError("symbolic model features differ from the preprocessor outputs")
    if len(Z) == 0:
        return np.zeros(0)
    scores = evaluate_expression(model.expression, Z, model.feature_names, model.operator_set)
    return model.a * scores + model.b


register_family(FAMILY, raw_score)
