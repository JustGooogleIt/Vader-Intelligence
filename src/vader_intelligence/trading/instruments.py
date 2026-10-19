"""Market metadata and correlation-group keys.

A `Market` is static metadata for one binary contract: its event, tick and quantity
grids, tradable price bounds, status and close time. Both contracts of one event form
one correlation group; groups whose dependence a model cannot support (default: the
same team on the same date) are merged into one cluster and capped jointly.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from .money import CENT, ONE, ZERO, dec, on_grid, opt_dec, opt_utc

STATUSES = ("open", "closed", "settled", "unknown")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")


def identifier(value, name="identifier"):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name}: invalid identifier {value!r}")
    return value


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str
    tick: object = CENT
    qty_step: object = ONE
    min_price: object = CENT
    max_price: object = None  # default: 1 - tick
    status: str = "open"
    close_time: datetime | None = None
    settlement_value: object = None
    teams: tuple = ()
    game_date: str | None = None

    def __post_init__(self):
        identifier(self.ticker, "ticker")
        identifier(self.event_ticker, "event_ticker")
        tick = dec(self.tick, "tick")
        step = dec(self.qty_step, "qty_step")
        if tick <= 0 or tick > ONE or step <= 0:
            raise ValueError("tick and qty_step must be positive (tick <= 1)")
        low = dec(self.min_price, "min_price")
        high = ONE - tick if self.max_price is None else dec(self.max_price, "max_price")
        if not (ZERO <= low <= high <= ONE) or not on_grid(low, tick) or not on_grid(high, tick):
            raise ValueError("price bounds must lie on the tick grid within [0, 1]")
        if self.status not in STATUSES:
            raise ValueError(f"unknown market status {self.status!r}")
        value = opt_dec(self.settlement_value, "settlement_value")
        if value is not None and not ZERO <= value <= ONE:
            raise ValueError("settlement_value must be within [0, 1]")
        teams = tuple(self.teams)
        if not all(isinstance(t, str) and t for t in teams):
            raise ValueError("teams must be non-empty strings")
        if self.game_date is not None and not isinstance(self.game_date, str):
            raise ValueError("game_date must be text")
        object.__setattr__(self, "tick", tick)
        object.__setattr__(self, "qty_step", step)
        object.__setattr__(self, "min_price", low)
        object.__setattr__(self, "max_price", high)
        object.__setattr__(self, "close_time", opt_utc(self.close_time, "close_time"))
        object.__setattr__(self, "settlement_value", value)
        object.__setattr__(self, "teams", teams)

    def is_open(self, now):
        """Tradable at `now`: status open and strictly before the close time."""
        return self.status == "open" and (self.close_time is None or now < self.close_time)


def same_team_same_date(a, b):
    """Default dependence predicate: two events share a team on the same game date."""
    return bool(a.game_date and a.game_date == b.game_date and set(a.teams) & set(b.teams))


def correlation_groups(markets, dependent=same_team_same_date):
    """Map each event ticker to its cluster key (smallest event ticker in the cluster).

    Every event is its own group; events linked by `dependent` (transitively) share
    one cluster and therefore one joint cap. Deterministic for a given market set.
    """
    markets = sorted(markets, key=lambda m: m.ticker)
    parent = {m.event_ticker: m.event_ticker for m in markets}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for i, a in enumerate(markets):
        for b in markets[i + 1 :]:
            if a.event_ticker != b.event_ticker and dependent(a, b):
                ra, rb = root(a.event_ticker), root(b.event_ticker)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return {event: root(event) for event in sorted(parent)}
