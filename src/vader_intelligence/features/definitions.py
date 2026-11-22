"""Versioned point-in-time feature definitions (roadmap B1).

Every feature is a named, versioned function of a `FeatureContext` built for one
opportunity at its cutoff C. The context holds only what was observable at C: quote
series already cut at C, and team results from official dates strictly before the
game's own official date. A feature returns a float or None (missing); its declared
missing-data treatment is applied later, inside model training, fit on training rows
only. `forward_capable` says whether forward (witnessed, collected) evidence can supply
the feature at C; a model using any feature that is not forward capable can never run in
the forward path and is labelled research-only.

Changing a feature's meaning requires a new version; feature sets list exact versions.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

QUOTE_MAX_AGE = 300  # seconds; the same limit the forward protocol applies to inputs at C
LOOKBACK_MAX_AGE = 300
WIN_PRIOR_GAMES = 10
POSTSEASON = {"F", "D", "L", "W"}


@dataclass(frozen=True)
class TeamGame:
    official_date: str
    won: bool
    runs_for: int
    runs_against: int
    game_type: str


@dataclass(frozen=True)
class FeatureContext:
    cutoff: datetime
    start: datetime
    official_date: str
    game_type: str
    selected_team: int
    opponent_team: int
    selected_is_home: bool
    selected: object  # QuoteSeries cut at the cutoff
    other: object  # QuoteSeries of the opposing contract cut at the cutoff
    selected_history: tuple = field(default_factory=tuple)  # TeamGame, before the date
    opponent_history: tuple = field(default_factory=tuple)

    def __post_init__(self):
        for series in (self.selected, self.other):
            if series is not None and any(q.at > self.cutoff for q in series.quotes):
                raise ValueError("feature context contains a quote after the cutoff")
        for history in (self.selected_history, self.opponent_history):
            if any(g.official_date >= self.official_date for g in history):
                raise ValueError("feature context contains a result from the game date or later")


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    version: int
    family: str
    missing: str
    forward_capable: bool
    description: str
    fn: object

    def describe(self):
        return {
            "name": self.name,
            "version": self.version,
            "family": self.family,
            "missing": self.missing,
            "forward_capable": self.forward_capable,
            "description": self.description,
        }


def _f(value):
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _logit(p):
    if p is None:
        return None
    p = min(max(float(p), 0.01), 0.99)
    return math.log(p / (1 - p))


def _mid(series, moment, max_age=QUOTE_MAX_AGE):
    if series is None:
        return None
    quote = series.latest(moment, max_age)
    return None if quote is None else quote.mid


def mid_c(ctx):
    return _f(_mid(ctx.selected, ctx.cutoff))


def spread_c(ctx):
    quote = ctx.selected.latest(ctx.cutoff, QUOTE_MAX_AGE)
    return None if quote is None else _f(quote.spread)


def pair_mid_c(ctx):
    own, other = _mid(ctx.selected, ctx.cutoff), _mid(ctx.other, ctx.cutoff)
    if own is None or other is None:
        return None
    return _f((own + (1 - other)) / 2)


def overround_c(ctx):
    own = ctx.selected.latest(ctx.cutoff, QUOTE_MAX_AGE)
    other = ctx.other.latest(ctx.cutoff, QUOTE_MAX_AGE) if ctx.other is not None else None
    if own is None or other is None or own.yes_ask is None or other.yes_ask is None:
        return None
    return _f(own.yes_ask + other.yes_ask - 1)


def _change(minutes):
    def fn(ctx):
        now = _mid(ctx.selected, ctx.cutoff)
        before = _mid(ctx.selected, ctx.cutoff - timedelta(minutes=minutes), LOOKBACK_MAX_AGE)
        if now is None or before is None:
            return None
        return _f(now - before)

    return fn


def last_trade_gap_c(ctx):
    now = _mid(ctx.selected, ctx.cutoff)
    if now is None:
        return None
    for quote in reversed(ctx.selected.quotes):
        if quote.trade_price is not None:
            if (ctx.cutoff - quote.at).total_seconds() > 3600:
                return None
            return _f(quote.trade_price - now)
    return None


def log_volume_60m(ctx):
    series, start = ctx.selected, ctx.cutoff - timedelta(minutes=60)
    if series.volume_kind == "period":
        window = [q.volume for q in series.quotes if q.at > start and q.volume is not None]
        if not window:
            return None
        return math.log1p(float(sum(window, Decimal(0))))
    latest = series.latest(ctx.cutoff, QUOTE_MAX_AGE)
    earlier = series.latest(start, LOOKBACK_MAX_AGE)
    if latest is None or earlier is None or latest.volume is None or earlier.volume is None:
        return None
    return math.log1p(max(0.0, float(latest.volume - earlier.volume)))


def log_open_interest_c(ctx):
    quote = ctx.selected.latest(ctx.cutoff, QUOTE_MAX_AGE)
    if quote is None or quote.open_interest is None:
        return None
    return math.log1p(float(quote.open_interest))


def _win_pct(history):
    wins = sum(1 for g in history if g.won)
    return (wins + 0.5 * WIN_PRIOR_GAMES) / (len(history) + WIN_PRIOR_GAMES)


def _run_diff(history):
    return sum(g.runs_for - g.runs_against for g in history) / (len(history) + WIN_PRIOR_GAMES)


def _last10(history):
    recent = history[-10:]
    if len(recent) < 3:
        return None
    return sum(1 for g in recent if g.won) / len(recent)


def _rest(history, date):
    if not history:
        return None
    gap = (datetime.fromisoformat(date) - datetime.fromisoformat(history[-1].official_date)).days
    return float(min(gap, 5))


def _season(history, date):
    return tuple(g for g in history if g.official_date[:4] == date[:4])


DEFINITIONS = {}


def define(name, version, family, missing, forward_capable, description, fn):
    key = f"{name}@{version}"
    if key in DEFINITIONS:
        raise ValueError(f"feature {key} defined twice")
    DEFINITIONS[key] = FeatureDefinition(
        name, version, family, missing, forward_capable, description, fn
    )


define(
    "mid_c", 1, "market", "median_indicator", True, "top-of-book YES mid at C (age ≤ 300 s)", mid_c
)
define(
    "logit_mid_c",
    1,
    "market",
    "median_indicator",
    True,
    "logit of mid_c clipped to [0.01, 0.99]",
    lambda c: _logit(mid_c(c)),
)
define(
    "spread_c", 1, "market", "median_indicator", True, "YES ask − bid at C (age ≤ 300 s)", spread_c
)
define(
    "pair_mid_c",
    1,
    "pair",
    "median_indicator",
    True,
    "mean of own mid and 1 − opposing contract's mid at C",
    pair_mid_c,
)
define(
    "logit_pair_mid_c",
    1,
    "pair",
    "median_indicator",
    True,
    "logit of pair_mid_c",
    lambda c: _logit(pair_mid_c(c)),
)
define(
    "overround_c", 1, "pair", "median_indicator", True, "sum of both YES asks − 1 at C", overround_c
)
define(
    "mid_change_15m",
    1,
    "market",
    "zero_indicator",
    True,
    "mid_c − mid at C−15 min (each ≤ 300 s old)",
    _change(15),
)
define(
    "mid_change_45m",
    1,
    "market",
    "zero_indicator",
    True,
    "mid_c − mid at C−45 min (each ≤ 300 s old)",
    _change(45),
)
define(
    "last_trade_gap_c",
    1,
    "market",
    "zero_indicator",
    True,
    "latest traded price within 60 min − mid_c",
    last_trade_gap_c,
)
define(
    "log_volume_60m",
    1,
    "market",
    "zero_indicator",
    True,
    "log(1 + contracts traded in the 60 min to C)",
    log_volume_60m,
)
define(
    "log_open_interest_c",
    1,
    "market",
    "median_indicator",
    True,
    "log(1 + open interest at C)",
    log_open_interest_c,
)
define(
    "selected_is_home",
    1,
    "context",
    "forbid",
    True,
    "1 if the selected YES team is at home",
    lambda c: 1.0 if c.selected_is_home else 0.0,
)
define(
    "postseason",
    1,
    "context",
    "forbid",
    True,
    "1 for a postseason game type",
    lambda c: 1.0 if c.game_type in POSTSEASON else 0.0,
)
define(
    "win_pct_diff",
    1,
    "team",
    "zero_indicator",
    False,
    "season win% (shrunk to .500 by 10 games) selected − opponent, prior dates only",
    lambda c: (
        _win_pct(_season(c.selected_history, c.official_date))
        - _win_pct(_season(c.opponent_history, c.official_date))
    ),
)
define(
    "run_diff_pg_diff",
    1,
    "team",
    "zero_indicator",
    False,
    "season run differential per game (shrunk) selected − opponent, prior dates only",
    lambda c: (
        _run_diff(_season(c.selected_history, c.official_date))
        - _run_diff(_season(c.opponent_history, c.official_date))
    ),
)
define(
    "last10_diff",
    1,
    "team",
    "zero_indicator",
    False,
    "last-10 win% selected − opponent (≥ 3 games each)",
    lambda c: (
        None
        if _last10(_season(c.selected_history, c.official_date)) is None
        or _last10(_season(c.opponent_history, c.official_date)) is None
        else _last10(_season(c.selected_history, c.official_date))
        - _last10(_season(c.opponent_history, c.official_date))
    ),
)
define(
    "rest_days_diff",
    1,
    "team",
    "zero_indicator",
    False,
    "days since previous game (cap 5) selected − opponent",
    lambda c: (
        None
        if _rest(_season(c.selected_history, c.official_date), c.official_date) is None
        or _rest(_season(c.opponent_history, c.official_date), c.official_date) is None
        else _rest(_season(c.selected_history, c.official_date), c.official_date)
        - _rest(_season(c.opponent_history, c.official_date), c.official_date)
    ),
)

FEATURE_SETS = {
    "fs-market-v1": (
        "mid_c@1",
        "logit_mid_c@1",
        "spread_c@1",
        "pair_mid_c@1",
        "logit_pair_mid_c@1",
        "overround_c@1",
        "mid_change_15m@1",
        "mid_change_45m@1",
        "last_trade_gap_c@1",
        "log_volume_60m@1",
        "log_open_interest_c@1",
        "selected_is_home@1",
        "postseason@1",
    ),
}
FEATURE_SETS["fs-full-v1"] = FEATURE_SETS["fs-market-v1"] + (
    "win_pct_diff@1",
    "run_diff_pg_diff@1",
    "last10_diff@1",
    "rest_days_diff@1",
)


def feature_set(name):
    """Definitions of a named feature set, in its fixed order."""
    if name not in FEATURE_SETS:
        raise ValueError(f"unknown feature set {name!r}")
    return [DEFINITIONS[key] for key in FEATURE_SETS[name]]


def describe_set(name):
    definitions = feature_set(name)
    return {
        "name": name,
        "features": [d.describe() for d in definitions],
        "forward_capable": all(d.forward_capable for d in definitions),
    }


def compute(name, ctx):
    """Feature values (float or None) of the set for one context, in the set's order."""
    values = {}
    for definition in feature_set(name):
        value = definition.fn(ctx)
        if value is not None:
            value = float(value)
            if not math.isfinite(value):
                value = None
        if value is None and definition.missing == "forbid":
            raise ValueError(f"feature {definition.name} may not be missing")
        values[definition.name] = value
    return values
