"""Exact values for trading: Decimal money, tick/step grids, timestamps and digests.

Floats are refused everywhere money or quantity flows. Prices are dollars per $1
contract; quantities are contract counts. Grid rounding is exact (integer division),
fees round up to their increment, payouts are never rounded. Every arithmetic entry
point runs under `exact()`, a fixed 40-digit context that traps invalid operations, so
results do not depend on a caller's global decimal context.
"""

import functools
import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from types import MappingProxyType

ZERO = Decimal(0)
ONE = Decimal(1)
CENT = Decimal("0.01")
QUANTUM = Decimal("1e-18")  # fixed exponent for divided amounts (cost basis), keeps sums exact
_TEXT = re.compile(r"-?\d{1,24}(?:\.\d{1,18})?\Z", re.ASCII)
_CONTEXT = Context(
    prec=40,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


def exact():
    """Fresh fixed-precision context; isolates callers' traps and precision."""
    return localcontext(_CONTEXT)


def in_exact(function):
    """Decorator running `function` under `exact()`."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with exact():
            return function(*args, **kwargs)

    return wrapper


def dec(value, name="value"):
    """Finite Decimal from a Decimal, int or bounded decimal string. Floats/bools refused."""
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name}: floats and booleans are not money")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        if not _TEXT.fullmatch(value):
            raise ValueError(f"{name}: expected a bounded decimal string, got {value!r}")
        result = Decimal(value)
    else:
        raise TypeError(f"{name}: unsupported type {type(value).__name__}")
    if not result.is_finite():
        raise ValueError(f"{name}: must be finite")
    return result


def opt_dec(value, name="value"):
    return None if value is None else dec(value, name)


def price(value, name="price"):
    """Binary contract price in [0, 1]."""
    result = dec(value, name)
    if not ZERO <= result <= ONE:
        raise ValueError(f"{name}: price must be within [0, 1]")
    return result


def nonnegative(value, name="value"):
    result = dec(value, name)
    if result < 0:
        raise ValueError(f"{name}: must be nonnegative")
    return result


def positive(value, name="value"):
    result = dec(value, name)
    if result <= 0:
        raise ValueError(f"{name}: must be positive")
    return result


@in_exact
def on_grid(value, step):
    return value % step == 0


@in_exact
def floor_to(value, step):
    """Largest multiple of `step` <= value (exact integer division)."""
    n = value // step
    if n * step > value:
        n -= 1
    return n * step


@in_exact
def ceil_to(value, step):
    """Smallest multiple of `step` >= value (exact integer division)."""
    n = value // step
    if n * step < value:
        n += 1
    return n * step


@in_exact
def fixed(value):
    """Quantize a divided amount to 18 decimals so later sums stay exact at 40 digits."""
    return value.quantize(QUANTUM)


@in_exact
def steps(quantity, step):
    """Number of `step` lots covering `quantity` (rounded up)."""
    return ceil_to(quantity, step) / step


def text(value):
    """Canonical decimal text: no exponent, no trailing zeros, '0' for zero."""
    if value is None:
        return None
    value = dec(value)
    if value == 0:
        return "0"
    with exact():
        return format(value.normalize(), "f")


def utc(value, name="timestamp"):
    """Aware UTC datetime from an aware datetime or ISO-8601 text; naive is refused."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError(f"{name}: expected a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}: naive timestamps are refused")
    return value.astimezone(timezone.utc)


def opt_utc(value, name="timestamp"):
    return None if value is None else utc(value, name)


def ts_text(value):
    return None if value is None else utc(value).isoformat()


def seconds(delta):
    """Exact Decimal seconds of a timedelta (microsecond resolution)."""
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    with exact():
        return Decimal(micros) / Decimal(1_000_000)


def plain(value):
    """JSON-safe structure with canonical Decimal and timestamp text; floats refused."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("floats are not permitted in trading records")
    if isinstance(value, Decimal):
        return text(value)
    if isinstance(value, datetime):
        return ts_text(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (dict, MappingProxyType)):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    raise TypeError(f"unsupported record value {type(value).__name__}")


def canonical(value):
    return json.dumps(plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def frozen_mapping(value, name="mapping"):
    """Read-only copy of a JSON-safe mapping (validated by encoding it once)."""
    if not isinstance(value, (dict, MappingProxyType)):
        raise TypeError(f"{name}: expected a mapping")
    canonical(value)
    return MappingProxyType(json.loads(canonical(value)))
