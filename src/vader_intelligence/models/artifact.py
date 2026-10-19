"""Model artifacts: canonical JSON, SHA-256 identity, write-once files, numpy inference.

The artifact file is the canonical JSON (`provenance.json_text`) of `to_dict()`, with
every float as a 17-significant-digit decimal string; its identity is the SHA-256 of
those exact bytes and its file name is `<sha256>.json`. Loading refuses anything whose
hash, canonical form, format version, family or contents do not verify.
"""

import hashlib
import json
import os
import platform
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import scipy
import sklearn

from ..provenance import json_text
from . import baselines, calibration, gbm, logistic
from ._common import encode
from .preprocess import FeatureSpec, Preprocessor

FORMAT = "model-artifact-v1"
FAMILIES = ("logistic", "gbm", "market_baseline", "symbolic")
KEYS = {
    "format",
    "family",
    "feature_names",
    "feature_specs",
    "preprocessor",
    "params",
    "calibration",
    "training",
    "parity",
}
SHA256 = re.compile(r"[0-9a-f]{64}")
FAMILY_NAME = re.compile(r"[a-z][a-z0-9_]*")

_BUILTIN = {
    "logistic": lambda params, Z, names: logistic.logit(params, Z),
    "gbm": lambda params, Z, names: gbm.raw_score(params, Z),
    "market_baseline": lambda params, Z, names: baselines.raw_score(params, Z),
}
_REGISTERED = {}


class ArtifactError(Exception):
    """An artifact failed verification or does not match its inputs; never a fallback."""


def register_family(name, raw_score_fn):
    """Plug in scoring for a family produced elsewhere (e.g. "symbolic").

    `raw_score_fn(params, Z, names)` returns one raw log-odds per row of the
    preprocessed matrix Z, whose columns are `names`; the artifact's calibration then
    maps those to probabilities. Built-in families cannot be replaced, and a name
    already registered to a different function is refused.
    """
    if not isinstance(name, str) or not FAMILY_NAME.fullmatch(name):
        raise ValueError(f"invalid family name {name!r}")
    if name in _BUILTIN:
        raise ValueError(f"family {name!r} is built in")
    if not callable(raw_score_fn):
        raise TypeError("raw_score_fn must be callable")
    if _REGISTERED.get(name, raw_score_fn) is not raw_score_fn:
        raise ValueError(f"family {name!r} is already registered to another function")
    _REGISTERED[name] = raw_score_fn


@dataclass(frozen=True)
class ModelArtifact:
    """A fitted model as data. Floats anywhere in params/calibration/training/parity
    become decimal strings at construction, so equal artifacts serialize identically.

    `training` is caller-supplied provenance (dataset manifest hash, split, config,
    seed, code hash, dependency versions); `parity` holds export-time parity evidence
    beyond the family params (logistic/gbm params carry `parity_max_abs_error`).
    """

    family: str
    preprocessor: Preprocessor
    params: dict
    calibration: dict
    training: dict
    parity: dict = field(default_factory=dict)
    format: str = FORMAT

    def __post_init__(self):
        if self.format != FORMAT:
            raise ArtifactError(f"unknown artifact format {self.format!r}")
        if not isinstance(self.family, str) or not _known(self.family):
            raise ArtifactError(f"unknown model family {self.family!r}")
        if not isinstance(self.preprocessor, Preprocessor):
            raise ArtifactError("preprocessor must be a Preprocessor")
        for name in ("params", "calibration", "training", "parity"):
            value = getattr(self, name)
            if not isinstance(value, dict):
                raise ArtifactError(f"{name} must be a dict")
            object.__setattr__(self, name, _canonical(value, name))
        try:
            calibration.apply(self.calibration, np.zeros(0))
            if self.family in _BUILTIN:  # validates params against the preprocessor width
                names = self.preprocessor.output_names
                _BUILTIN[self.family](self.params, np.zeros((0, len(names))), names)
        except (ValueError, TypeError, KeyError) as exc:
            raise ArtifactError(f"invalid {self.family} artifact: {exc}") from exc

    @property
    def feature_names(self):
        return self.preprocessor.input_names

    @property
    def feature_specs(self):
        return self.preprocessor.specs

    def to_dict(self):
        return json.loads(self.to_json())

    def to_json(self):
        return json_text(
            {
                "format": self.format,
                "family": self.family,
                "feature_names": self.feature_names,
                "feature_specs": [spec.to_dict() for spec in self.feature_specs],
                "preprocessor": self.preprocessor.to_dict(),
                "params": self.params,
                "calibration": self.calibration,
                "training": self.training,
                "parity": self.parity,
            }
        )

    @property
    def sha256(self):
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def save(self, directory):
        """Write `<sha256>.json` once: identical existing content is a no-op, anything else
        at that path is refused. Returns the path."""
        data = self.to_json().encode()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{hashlib.sha256(data).hexdigest()}.json"
        if not path.exists():
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".artifact-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.link(tmp, path)  # atomic and exclusive: never replaces an existing file
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
        if path.read_bytes() != data:
            raise ArtifactError(f"refusing to overwrite {path} with different content")
        return path

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ArtifactError("artifact must be a JSON object")
        if data.get("format") != FORMAT:
            raise ArtifactError(f"unknown artifact format {data.get('format')!r}")
        if set(data) != KEYS:
            raise ArtifactError(f"artifact keys {sorted(data)} != {sorted(KEYS)}")
        if not isinstance(data["family"], str) or not _known(data["family"]):
            raise ArtifactError(f"unknown model family {data['family']!r}")
        try:
            preprocessor = Preprocessor.from_dict(data["preprocessor"])
            if not isinstance(data["feature_specs"], list):
                raise ValueError("feature_specs must be a list")
            specs = tuple(FeatureSpec.from_dict(item) for item in data["feature_specs"])
        except (ValueError, TypeError) as exc:
            raise ArtifactError(f"invalid preprocessor: {exc}") from exc
        if specs != preprocessor.specs or data["feature_names"] != preprocessor.input_names:
            raise ArtifactError("feature_names/feature_specs disagree with the preprocessor")
        return cls(
            family=data["family"],
            preprocessor=preprocessor,
            params=data["params"],
            calibration=data["calibration"],
            training=data["training"],
            parity=data["parity"],
        )


def load(path, expected_sha256=None):
    """Load and verify an artifact file.

    The SHA-256 of the bytes must equal `expected_sha256` when given and the file name
    when it is `<sha256>.json`; at least one of the two is required. The bytes must be
    canonical JSON that round-trips through `ModelArtifact` unchanged.
    """
    path = Path(path)
    if expected_sha256 is not None and not (
        isinstance(expected_sha256, str) and SHA256.fullmatch(expected_sha256)
    ):
        raise ArtifactError("expected_sha256 must be 64 lowercase hex characters")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    stem = path.name.removesuffix(".json")
    named = SHA256.fullmatch(stem) is not None
    if expected_sha256 is None and not named:
        raise ArtifactError("nothing to verify against: pass expected_sha256")
    if (expected_sha256 is not None and digest != expected_sha256) or (named and digest != stem):
        raise ArtifactError(f"hash mismatch for {path}: content is {digest}")
    try:
        obj = json.loads(data.decode("utf-8"))
        canonical = json_text(obj).encode()
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArtifactError(f"{path} is not valid JSON") from exc
    if canonical != data:
        raise ArtifactError(f"{path} is not canonical JSON")
    artifact = ModelArtifact.from_dict(obj)
    if artifact.to_json().encode() != data:
        raise ArtifactError(f"{path} does not round-trip through ModelArtifact")
    return artifact


def check_inputs(artifact, feature_names):
    """Raise unless `feature_names` is exactly the artifact's input columns, in order."""
    if list(feature_names) != artifact.feature_names:
        raise ArtifactError(
            f"feature mismatch: expected {artifact.feature_names}, got {list(feature_names)}"
        )


def predict_proba(artifact, X):
    """preprocess -> family raw log-odds -> calibration. X columns follow feature_names."""
    X = np.asarray(X, dtype=np.float64)
    if X.ndim != 2 or X.shape[1] != len(artifact.feature_names):
        raise ArtifactError(
            f"feature mismatch: expected {len(artifact.feature_names)} columns, got {X.shape}"
        )
    Z = artifact.preprocessor.transform(X)
    raw = _scorer(artifact.family)(artifact.params, Z, artifact.preprocessor.output_names)
    raw = np.asarray(raw, dtype=np.float64)
    if raw.shape != (len(X),) or not np.isfinite(raw).all():
        raise ArtifactError(f"{artifact.family} scorer returned invalid raw scores")
    return calibration.apply(artifact.calibration, raw)


def dependency_versions():
    """Versions that determine fitted floats, for the caller's `training` record."""
    return {
        "numpy": np.__version__,
        "python": platform.python_version(),
        "scikit-learn": sklearn.__version__,
        "scipy": scipy.__version__,
    }


def _known(family):
    return family in FAMILIES or family in _REGISTERED


def _scorer(family):
    fn = _BUILTIN.get(family) or _REGISTERED.get(family)
    if fn is None:
        raise ArtifactError(f"no scorer registered for family {family!r}")
    return fn


def _canonical(value, path):
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ArtifactError(f"{path}: keys must be strings")
        return {key: _canonical(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_canonical(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        try:
            return encode(value)
        except ValueError as exc:
            raise ArtifactError(f"{path}: {exc}") from exc
    raise ArtifactError(f"{path}: unsupported value of type {type(value).__name__}")
