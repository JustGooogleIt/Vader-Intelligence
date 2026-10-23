"""Decimal scores: precision 40, ROUND_HALF_EVEN, no ambient-context dependence."""

from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

from ..normalize import DECIMAL

EPSILON = Decimal("0.000001")


def arithmetic():
    """A fresh context also isolates traps and exponent limits set by callers."""
    return localcontext(
        Context(
            prec=40,
            rounding=ROUND_HALF_EVEN,
            Emin=-999999,
            Emax=999999,
            capitals=1,
            clamp=0,
            flags=[],
            traps=[InvalidOperation, DivisionByZero, Overflow],
        )
    )


def probability(value):
    """Accept exact fixed-point strings (<=40 chars) or equivalent finite Decimals.

    Floats, integers/bools, signs and exponent strings are not source probabilities.
    Decimal inputs are bounded before fixed-point conversion to avoid huge expansions.
    """
    if isinstance(value, Decimal):
        if not value.is_finite() or value.is_signed():
            raise ValueError("probability must be finite and nonnegative")
        parts = value.as_tuple()
        if len(parts.digits) > 40 or not -38 <= parts.exponent <= 0:
            raise ValueError("probability exceeds fixed-point input bounds")
        value = format(value, "f")
    if not isinstance(value, str) or len(value) > 40 or not DECIMAL.fullmatch(value):
        raise ValueError("probability must be an exact bounded decimal string or Decimal")
    number = Decimal(value)
    if not 0 <= number <= 1:
        raise ValueError("probability must be in [0,1]")
    return number


def binary_label(y):
    if type(y) is not int or y not in (0, 1):
        raise ValueError("binary label must be integer 0 or 1")
    return y


def brier(p, y):
    """Single-Bernoulli Brier score, without clipping or a factor of two."""
    p, y = probability(p), binary_label(y)
    with arithmetic():
        return (p - y) ** 2


def clipped_probability(p):
    """v1 log-loss probability; compare with p to count clips. Never overwrite p."""
    p = probability(p)
    with arithmetic():
        return min(1 - EPSILON, max(EPSILON, p))


def log_loss(p, y):
    """Natural-log loss using v1 epsilon; returns an unformatted Decimal score."""
    q, y = clipped_probability(p), binary_label(y)
    with arithmetic():
        return -(q if y else 1 - q).ln()


def score_pair(p, y):
    """Both v1 scores for one probability as exact decimal strings, plus whether the
    log-loss input was clipped. `p` itself is never modified."""
    original = probability(p)
    clipped = clipped_probability(original) != original
    return {
        "brier": format(brier(original, y), "f"),
        "log_loss": format(log_loss(original, y), "f"),
    }, clipped


def mean(values):
    """Decimal mean of exact score strings; None for an empty sample (never 0 or NaN)."""
    values = list(values)
    if not values:
        return None
    try:
        with arithmetic():
            total = sum((Decimal(v) for v in values), Decimal(0))
            return format(total / len(values), "f")
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("scores must be exact finite decimal strings") from exc


def paired_summary(items):
    """Paired constant/midpoint summary over items that carry both score pairs.

    Each item is a mapping with "constant-v1" and "midpoint-v1" score dicts (or None).
    Only items with both scores are paired; N=0 yields null means. Deltas are
    midpoint − constant per game (negative favours midpoint), averaged with weight
    1/game. Returns exact decimal strings.
    """
    paired = [
        item
        for item in items
        if item.get("constant-v1") is not None and item.get("midpoint-v1") is not None
    ]
    summary = {"n": len(paired)}
    for metric in ("brier", "log_loss"):
        constant = [item["constant-v1"][metric] for item in paired]
        midpoint = [item["midpoint-v1"][metric] for item in paired]
        with arithmetic():
            deltas = [format(Decimal(m) - Decimal(c), "f") for c, m in zip(constant, midpoint)]
        summary[f"mean_{metric}_constant"] = mean(constant)
        summary[f"mean_{metric}_midpoint"] = mean(midpoint)
        summary[f"mean_{metric}_delta"] = mean(deltas)
    return summary
