"""The `symbolic` family for model artifacts: raw log-odds a·expr(Z) + b (roadmap C).

The artifact's preprocessor produces Z; the stored engine model evaluates its expression
on Z's columns (which must be exactly the model's feature names) with the same code the
search used, so artifact probabilities equal the engine's replay. Registered on import.
"""

from ..models.artifact import ArtifactError, register_family
from .expressions import evaluate_expression
from .model import model_from_dict

FAMILY = "symbolic"


def raw_score(params, Z, names):
    model = model_from_dict(params["model"])
    if list(model.feature_names) != list(names):
        raise ArtifactError("symbolic model features differ from the preprocessor outputs")
    scores = evaluate_expression(model.expression, Z, model.feature_names, model.operator_set)
    return model.a * scores + model.b


register_family(FAMILY, raw_score)
