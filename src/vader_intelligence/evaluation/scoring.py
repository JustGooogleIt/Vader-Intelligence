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
