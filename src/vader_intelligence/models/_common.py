"""Shared numerics: canonical decimal strings, input validation, stable sigmoid."""

from contextlib import contextmanager

import numpy as np
from threadpoolctl import threadpool_limits  # a scikit-learn dependency

SIGNIFICANT_DIGITS = 17
PARITY_TOLERANCE = 1e-9


def encode(value, *, allow_inf=False):
    """Float as a 17-significant-digit decimal string; exact float64 round trip.

    NaN is never encoded; infinities only where a caller explicitly allows them.
    """
    if isinstance(value, (bool, np.bool_)):
        raise TypeError("booleans are not numbers here")
    number = float(value)
    if np.isnan(number) or (np.isinf(number) and not allow_inf):
        raise ValueError(f"cannot encode non-finite number {number!r}")
    return format(number, f".{SIGNIFICANT_DIGITS}g")


def decode(text, *, allow_inf=False):
    """Inverse of `encode`; refuses anything that is not exactly its canonical output."""
    if not isinstance(text, str):
        raise ValueError(f"expected a decimal string, got {type(text).__name__}")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"not a decimal string: {text!r}") from exc
    if encode(number, allow_inf=allow_inf) != text:
        raise ValueError(f"non-canonical decimal string: {text!r}")
    return number


def encode_array(values, *, allow_inf=False):
    return [encode(v, allow_inf=allow_inf) for v in np.asarray(values, dtype=np.float64)]


def decode_array(texts, *, allow_inf=False):
    if not isinstance(texts, list):
        raise ValueError("expected a list of decimal strings")
    return np.array([decode(t, allow_inf=allow_inf) for t in texts], dtype=np.float64)


def as_matrix(X, *, allow_nan, name="X"):
    """2-D float64 array; infinities always refused, NaN only when allowed."""
    X = np.asarray(X, dtype=np.float64)
    if X.ndim != 2:
        raise ValueError(f"{name} must be 2-D, got shape {X.shape}")
    if np.isinf(X).any():
        raise ValueError(f"{name} contains infinite values")
    if not allow_nan and np.isnan(X).any():
        raise ValueError(f"{name} contains NaN")
    return X


def as_labels(y, n, *, both_classes=False):
    """1-D int64 labels in {0, 1}; integer or boolean dtype only (no float labels)."""
    y = np.asarray(y)
    if y.ndim != 1 or len(y) != n:
        raise ValueError(f"labels must be 1-D with {n} entries, got shape {y.shape}")
    if y.dtype.kind not in "iub":
        raise ValueError(f"labels must have integer or boolean dtype, got {y.dtype}")
    y = y.astype(np.int64)
    if not np.isin(y, (0, 1)).all():
        raise ValueError("labels must be 0 or 1")
    if both_classes and len(np.unique(y)) != 2:
        raise ValueError("labels must contain both classes")
    return y


def as_vector(values, n=None, *, name="values"):
    """1-D finite float64 array (optionally of length n)."""
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 1 or (n is not None and len(values) != n):
        raise ValueError(f"{name} must be 1-D with {n} entries, got shape {values.shape}")
    if not np.isfinite(values).all():
        raise ValueError(f"{name} must be finite")
    return values


def as_probabilities(p, n=None):
    p = as_vector(p, n, name="probabilities")
    if ((p < 0) | (p > 1)).any():
        raise ValueError("probabilities must be in [0, 1]")
    return p


def as_groups(groups, n):
    """1-D integer or string group labels, one per row."""
    groups = np.asarray(groups)
    if groups.ndim != 1 or len(groups) != n:
        raise ValueError(f"groups must be 1-D with {n} entries, got shape {groups.shape}")
    if groups.dtype.kind not in "iuUS":
        raise ValueError(f"groups must be integers or strings, got dtype {groups.dtype}")
    return groups


def as_seed(seed):
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative int")
    return seed


def sigmoid(z):
    """Overflow-free logistic function."""
    z = np.asarray(z, dtype=np.float64)
    out = np.empty_like(z)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    e = np.exp(z[~positive])
    out[~positive] = e / (1.0 + e)
    return out


def logit(p):
    p = np.asarray(p, dtype=np.float64)
    return np.log(p) - np.log1p(-p)


@contextmanager
def single_threaded():
    """Pin BLAS/OpenMP to one thread so fitted floats do not depend on core count."""
    with threadpool_limits(limits=1):
        yield
