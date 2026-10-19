"""Feature preprocessing fit on training rows only, serialized with its learned values."""

from dataclasses import dataclass

import numpy as np

from ._common import as_matrix, decode, encode

MISSING = ("median_indicator", "zero_indicator", "forbid")
STD_FLOOR = 1e-12
INDICATOR_SUFFIX = "__missing"


@dataclass(frozen=True)
class FeatureSpec:
    """One input column and its declared missing-value treatment."""

    name: str
    missing: str = "median_indicator"
    scale: bool = True

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("feature name must be a nonempty string")
        if self.missing not in MISSING:
            raise ValueError(f"feature {self.name!r}: unknown missing treatment {self.missing!r}")
        if type(self.scale) is not bool:
            raise ValueError(f"feature {self.name!r}: scale must be a bool")

    @property
    def indicator(self):
        return self.missing != "forbid"

    def to_dict(self):
        return {"name": self.name, "missing": self.missing, "scale": self.scale}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != {"name", "missing", "scale"}:
            raise ValueError(f"malformed feature spec: {data!r}")
        return cls(data["name"], data["missing"], data["scale"])


@dataclass(frozen=True)
class Preprocessor:
    """Impute, standardize, then append missing indicators (in spec order).

    Per feature: `fill` is the imputation value (training median of observed values, 0
    when none were observed; 0 for zero_indicator; None for forbid). `mean`/`std` are
    training statistics of the imputed column (None when scale=False). A column with
    std < 1e-12 is marked `constant`: it is centered but never divided by its std.
    """

    specs: tuple
    fill: tuple
    mean: tuple
    std: tuple
    constant: tuple

    def __post_init__(self):
        specs = tuple(self.specs)
        if not specs or not all(isinstance(s, FeatureSpec) for s in specs):
            raise ValueError("specs must be a nonempty sequence of FeatureSpec")
        object.__setattr__(self, "specs", specs)
        for field in ("fill", "mean", "std", "constant"):
            values = tuple(getattr(self, field))
            if len(values) != len(specs):
                raise ValueError(f"{field} must have one entry per feature")
            object.__setattr__(self, field, values)
        for spec, fill, mean, std, constant in zip(
            specs, self.fill, self.mean, self.std, self.constant
        ):
            _check_feature(spec, fill, mean, std, constant)
        names = self.output_names
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate feature or indicator names: {names}")

    @classmethod
    def fit(cls, X, specs):
        """Learn imputation and scaling from these (training) rows only."""
        specs = tuple(specs)
        X = _checked(X, specs)
        if len(X) == 0:
            raise ValueError("cannot fit a preprocessor on zero rows")
        fill, mean, std, constant = [], [], [], []
        for j, spec in enumerate(specs):
            column = X[:, j]
            missing = np.isnan(column)
            if spec.missing == "median_indicator":
                value = float(np.median(column[~missing])) if (~missing).any() else 0.0
            elif spec.missing == "zero_indicator":
                value = 0.0
            else:
                value = None
            filled = column if value is None else np.where(missing, value, column)
            fill.append(value)
            if spec.scale:
                mean.append(float(filled.mean()))
                std.append(float(filled.std()))
                constant.append(std[-1] < STD_FLOOR)
            else:
                mean.append(None)
                std.append(None)
                constant.append(False)
        return cls(specs, tuple(fill), tuple(mean), tuple(std), tuple(constant))

    @property
    def input_names(self):
        return [s.name for s in self.specs]

    @property
    def output_names(self):
        return self.input_names + [s.name + INDICATOR_SUFFIX for s in self.specs if s.indicator]

    def transform(self, X):
        """Apply the stored training values; NaN in a `forbid` column is an error."""
        X = _checked(X, self.specs)
        columns, indicators = [], []
        for j, spec in enumerate(self.specs):
            column = X[:, j]
            missing = np.isnan(column)
            if spec.indicator:
                column = np.where(missing, self.fill[j], column)
                indicators.append(missing.astype(np.float64))
            if spec.scale:
                column = column - self.mean[j]
                if not self.constant[j]:
                    column = column / self.std[j]
            columns.append(column)
        return np.column_stack(columns + indicators)

    def to_dict(self):
        features = []
        for spec, fill, mean, std, constant in zip(
            self.specs, self.fill, self.mean, self.std, self.constant
        ):
            features.append(
                spec.to_dict()
                | {
                    "fill": None if fill is None else encode(fill),
                    "mean": None if mean is None else encode(mean),
                    "std": None if std is None else encode(std),
                    "constant": constant,
                }
            )
        return {"features": features, "output_names": self.output_names}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != {"features", "output_names"}:
            raise ValueError("malformed preprocessor")
        if not isinstance(data["features"], list):
            raise ValueError("malformed preprocessor features")
        keys = {"name", "missing", "scale", "fill", "mean", "std", "constant"}
        specs, fill, mean, std, constant = [], [], [], [], []
        for item in data["features"]:
            if not isinstance(item, dict) or set(item) != keys:
                raise ValueError(f"malformed preprocessor feature: {item!r}")
            specs.append(FeatureSpec(item["name"], item["missing"], item["scale"]))
            for out, key in ((fill, "fill"), (mean, "mean"), (std, "std")):
                out.append(None if item[key] is None else decode(item[key]))
            constant.append(item["constant"])
        result = cls(tuple(specs), tuple(fill), tuple(mean), tuple(std), tuple(constant))
        if data["output_names"] != result.output_names:
            raise ValueError("preprocessor output_names do not match its features")
        return result


def _checked(X, specs):
    X = as_matrix(X, allow_nan=True)
    if X.shape[1] != len(specs):
        raise ValueError(f"X has {X.shape[1]} columns, expected {len(specs)}")
    for j, spec in enumerate(specs):
        if spec.missing == "forbid" and np.isnan(X[:, j]).any():
            raise ValueError(f"feature {spec.name!r}: NaN where the treatment is 'forbid'")
    return X


def _check_feature(spec, fill, mean, std, constant):
    name = spec.name
    if type(constant) is not bool:
        raise ValueError(f"feature {name!r}: constant must be a bool")
    if spec.missing == "forbid":
        if fill is not None:
            raise ValueError(f"feature {name!r}: forbid has no fill value")
    elif type(fill) is not float or not np.isfinite(fill):
        raise ValueError(f"feature {name!r}: fill must be a finite float")
    elif spec.missing == "zero_indicator" and fill != 0.0:
        raise ValueError(f"feature {name!r}: zero_indicator fill must be 0")
    if not spec.scale:
        if mean is not None or std is not None or constant:
            raise ValueError(f"feature {name!r}: unscaled features carry no statistics")
        return
    for value in (mean, std):
        if type(value) is not float or not np.isfinite(value):
            raise ValueError(f"feature {name!r}: mean and std must be finite floats")
    if std < 0 or constant != (std < STD_FLOOR):
        raise ValueError(f"feature {name!r}: inconsistent std/constant")
