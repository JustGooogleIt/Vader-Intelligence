"""Kalshi NFL/NBA candidate identity from the event ticker, team UUIDs and rule text."""

import re
from dataclasses import dataclass
from datetime import date

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    )
}
RULE_DATE = re.compile(r"originally scheduled for ([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")


@dataclass(frozen=True)
class Candidate:
    league: str
    event_ticker: str
    et_date: date  # US Eastern date encoded in the ticker
    away: object  # Team
    home: object  # Team
    markets: tuple  # (team code, market ticker), away first


@dataclass(frozen=True)
class Rejection:
    reason: str


def parse_event_ticker(league, event_ticker):
    """`KXNFLGAME-26NOV30CARTB` → (date(2026, 11, 30), away Team, home Team), or a
    Rejection. Team codes have no separator, so the split must be unique among the
    league's known codes."""
    prefix = league.series + "-"
    if not isinstance(event_ticker, str) or not event_ticker.startswith(prefix):
        return Rejection("wrong_series")
    match = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{2})([A-Z]{4,8})", event_ticker[len(prefix) :])
    if not match or match[2] not in MONTHS:
        return Rejection("unparseable_ticker")
    try:
        day = date(2000 + int(match[1]), MONTHS[match[2]], int(match[3]))
    except ValueError:
        return Rejection("unparseable_ticker")
    codes = match[4]
    splits = [
        (league.by_code(codes[:i]), league.by_code(codes[i:]))
        for i in range(2, len(codes) - 1)
        if league.by_code(codes[:i]) and league.by_code(codes[i:])
    ]
    if not splits:
        return Rejection("unknown_team")
    if len(splits) > 1:
        return Rejection("ambiguous_team_codes")
    away, home = splits[0]
    if away.code == home.code:
        return Rejection("unknown_team")
    return day, away, home


def candidate(league, event_ticker, markets):
    """Validate one Kalshi event and its markets (dicts as listed by the API).

    Requires exactly two active markets, one per team, each carrying that team's UUID
    in `custom_strike`, and rule text naming the league's sport and the ticker's date.
    """
    parsed = parse_event_ticker(league, event_ticker)
    if isinstance(parsed, Rejection):
        return parsed
    day, away, home = parsed
    if not isinstance(markets, (list, tuple)) or len(markets) != 2:
        return Rejection("ambiguous_contract_family")
    seen = {}
    for market in markets:
        ticker = market.get("ticker") if isinstance(market, dict) else None
        if not isinstance(ticker, str) or not ticker.startswith(event_ticker + "-"):
            return Rejection("ambiguous_contract_family")
        code = ticker[len(event_ticker) + 1 :]
        team = {away.code: away, home.code: home}.get(code)
        if team is None or code in seen:
            return Rejection("ambiguous_contract_family")
        strike = market.get("custom_strike")
        if not isinstance(strike, dict) or strike.get(league.strike_key) != team.uuid:
            return Rejection("team_identity_mismatch")
        if market.get("status") != "active":
            return Rejection("market_not_active")
        rule = market.get("rules_primary")
        found = RULE_DATE.search(rule) if isinstance(rule, str) else None
        if not found or league.sport_phrase not in rule:
            return Rejection("unrecognized_rule")
        month = MONTHS.get(found[1].upper())
        if month is None or date(int(found[3]), month, int(found[2])) != day:
            return Rejection("rule_date_mismatch")
        seen[code] = ticker
    return Candidate(
        league=league.key,
        event_ticker=event_ticker,
        et_date=day,
        away=away,
        home=home,
        markets=((away.code, seen[away.code]), (home.code, seen[home.code])),
    )
