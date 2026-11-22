"""Parameter-free reference models over one named input column, scored like any artifact.

Both are `market_baseline` artifacts with a pass-through preprocessor (missing values
forbidden, no scaling) and identity calibration, so they go through `predict_proba`
exactly as fitted models do.
"""

import numpy as np

from ._common import as_matrix, decode, encode, logit
from .calibration import identity
from .preprocess import FeatureSpec, Preprocessor

CLIP = 1e-6


def market_mid(column, *, training=None):
    """Probability = the column (a midpoint in [0, 1]) clipped to [1e-6, 1 - 1e-6]."""
    return _artifact(column, {"kind": "market_mid", "clip": encode(CLIP)}, training)


def constant(column, *, training=None):
    """Probability 0.5 for every row; the column only fixes the input interface."""
    return _artifact(column, {"kind": "constant", "probability": encode(0.5)}, training)


def raw_score(params, Z):
    """Raw log-odds for a one-column Z: logit of the clipped midpoint, or 0 (p = 0.5)."""
    Z = as_matrix(Z, allow_nan=False, name="Z")
    if Z.shape[1] != 1:
        raise ValueError(f"market baselines take exactly one column, got {Z.shape[1]}")
    kind = params.get("kind") if isinstance(params, dict) else None
    if kind == "market_mid" and set(params) == {"kind", "clip"}:
        clip = decode(params["clip"])
        if not 0 < clip < 0.5:
            raise ValueError("clip must be in (0, 0.5)")
        mid = Z[:, 0]
        if ((mid < 0) | (mid > 1)).any():
            raise ValueError("market midpoints must be in [0, 1]")
        return logit(np.clip(mid, clip, 1 - clip))
    if kind == "constant" and set(params) == {"kind", "probability"}:
        if decode(params["probability"]) != 0.5:
            raise ValueError("the constant baseline is defined at probability 0.5")
        return np.zeros(len(Z))
    raise ValueError(f"malformed market_baseline params: {params!r}")


def _artifact(column, params, training):
    from .artifact import ModelArtifact  # deferred: artifact imports this module to score

    spec = FeatureSpec(column, missing="forbid", scale=False)
    preprocessor = Preprocessor((spec,), (None,), (None,), (None,), (False,))
    training = {} if training is None else training
    return ModelArtifact("market_baseline", preprocessor, params, identity(), training)
