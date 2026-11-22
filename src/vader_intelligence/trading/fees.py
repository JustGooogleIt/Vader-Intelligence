"""Versioned fee schedules: data, provenance and assumptions; fee per fill.

    fee = round_up(rate x fee_multiplier x quantity x price x (1 - price), increment)

with `rate` the taker or maker rate of the fill's liquidity. A schedule is pure data:
changing numbers means shipping a new version, never editing code paths. The schedule
used by a run is stored in its manifest together with its verification flag.
"""

from dataclasses import dataclass, replace

from .money import CENT, ONE, ceil_to, dec, digest, in_exact, nonnegative, plain, price

LIQUIDITY = ("maker", "taker")
ROUNDING = ("up_per_fill",)
_HALF = dec("0.5")


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    source_url: str | None
    retrieved_at: str | None
    verified: bool
    assumptions: tuple
    taker_rate: object
    maker_rate: object
    fee_multiplier: object = ONE
    rounding_increment: object = CENT
    rounding: str = "up_per_fill"

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("fee schedule version is required")
        if not isinstance(self.verified, bool):
            raise TypeError("verified must be a bool")
        assumptions = tuple(self.assumptions)
        if not all(isinstance(a, str) and a for a in assumptions):
            raise ValueError("assumptions must be non-empty strings")
        if not self.verified and not assumptions:
            raise ValueError("an unverified schedule must state its assumptions")
        for name in ("taker_rate", "maker_rate", "fee_multiplier"):
            value = nonnegative(getattr(self, name), name)
            if value > ONE:
                raise ValueError(f"{name} must be within [0, 1]")
            object.__setattr__(self, name, value)
        increment = dec(self.rounding_increment, "rounding_increment")
        if increment <= 0 or increment > ONE:
            raise ValueError("rounding_increment must be within (0, 1]")
        if self.rounding not in ROUNDING:
            raise ValueError(f"unsupported rounding {self.rounding!r}")
        object.__setattr__(self, "rounding_increment", increment)
        object.__setattr__(self, "assumptions", assumptions)

    def rate(self, liquidity):
        if liquidity not in LIQUIDITY:
            raise ValueError(f"unknown liquidity {liquidity!r}")
        return self.taker_rate if liquidity == "taker" else self.maker_rate

    @in_exact
    def fee(self, fill_price, quantity, liquidity):
        """Fee of one fill, rounded up to the schedule's increment."""
        p = price(fill_price)
        q = nonnegative(quantity, "quantity")
        raw = self.rate(liquidity) * self.fee_multiplier * q * p * (ONE - p)
        return ceil_to(raw, self.rounding_increment)

    @in_exact
    def fee_per_contract(self, fill_price, liquidity, qty_step=ONE):
        """Per-contract fee used for edge and Kelly: the rounded fee of a lot of
        max(step, 1) contracts divided by its size (fractional steps are priced per
        whole contract, not per tiny lot)."""
        lot = max(dec(qty_step, "qty_step"), ONE)
        return self.fee(fill_price, lot, liquidity) / lot

    @in_exact
    def fee_bound_per_step(self, limit_price, action, qty_step=ONE, liquidity=None):
        """Upper bound on the fee of one step lot filled at any price the limit allows.

        Buys fill at or below the limit, sells at or above; p(1 - p) peaks at 0.5. Fees
        round up per fill, so k lots filled in any partition of fills cost at most k
        times this bound. `liquidity=None` takes the larger of the two rates.
        """
        limit = price(limit_price, "limit_price")
        if action == "buy":
            worst = min(limit, _HALF)
        elif action == "sell":
            worst = max(limit, _HALF)
        else:
            raise ValueError(f"unknown action {action!r}")
        rate = max(self.taker_rate, self.maker_rate) if liquidity is None else self.rate(liquidity)
        raw = rate * self.fee_multiplier * dec(qty_step, "qty_step") * worst * (ONE - worst)
        return ceil_to(raw, self.rounding_increment)

    def to_dict(self):
        return plain(self)

    def digest(self):
        return digest(self.to_dict())

    def manifest(self):
        """What a run manifest records about the schedule it used."""
        return {
            "version": self.version,
            "verified": self.verified,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "assumptions": list(self.assumptions),
            "digest": self.digest(),
        }


def fee_schedule_from_dict(data):
    return FeeSchedule(
        version=data["version"],
        source_url=data.get("source_url"),
        retrieved_at=data.get("retrieved_at"),
        verified=data["verified"],
        assumptions=tuple(data["assumptions"]),
        taker_rate=dec(data["taker_rate"]),
        maker_rate=dec(data["maker_rate"]),
        fee_multiplier=dec(data.get("fee_multiplier", "1")),
        rounding_increment=dec(data.get("rounding_increment", "0.01")),
        rounding=data.get("rounding", "up_per_fill"),
    )


_COMMON = (
    "Formula and rates (taker 0.07, maker 0.0175, rounded up to the cent, no settlement "
    "fee) are as read by the integrator from an archived copy of the February 2026 Kalshi "
    "fee schedule; not verified in this repository (verified=False).",
    "Source URL and retrieval time are not recorded yet; the integrator records them when "
    "the schedule is verified.",
    "Rounding up is applied per fill at the stated increment; the venue's rounding unit "
    "(per fill, order or trade) and increment (current docs mention $0.0001 for direct "
    "members) are schedule parameters, not verified.",
    "No fee caps, rebates, volume tiers or per-market overrides are modelled.",
)

KALSHI_2026_02_GENERAL = FeeSchedule(
    version="kalshi-2026-02-general",
    source_url=None,
    retrieved_at=None,
    verified=False,
    assumptions=_COMMON
    + ("fee_multiplier 1: the general schedule; market fee multipliers are not applied.",),
    taker_rate=dec("0.07"),
    maker_rate=dec("0.0175"),
)

KALSHI_2026_02_MLB_ASSUMED = replace(
    KALSHI_2026_02_GENERAL,
    version="kalshi-2026-02-mlb-multiplier-0.5-assumed",
    fee_multiplier=dec("0.5"),
    assumptions=_COMMON
    + (
        "KXMLBGAME markets report fee_type quadratic_with_maker_fees and fee_multiplier "
        "0.5. ASSUMED: the multiplier scales both rates (effective taker 0.035, maker "
        "0.00875); how fee_multiplier applies is not documented in the material read.",
    ),
)

# Paper runs default to the higher-fee schedule: the conservative choice while the
# multiplier semantics are unverified.
DEFAULT_FEE_SCHEDULE = KALSHI_2026_02_GENERAL
PROVISIONAL_SCHEDULES = (KALSHI_2026_02_GENERAL, KALSHI_2026_02_MLB_ASSUMED)
