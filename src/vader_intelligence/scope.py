"""Conservative MLB identity matching; every alias below is explicitly reviewed."""

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .normalize import decimal_value

TEAMS = {
    109: ("AZ", "ARI", "Arizona", "Arizona Diamondbacks"),
    144: ("ATL", "Atlanta", "Atlanta Braves"),
    110: ("BAL", "Baltimore", "Baltimore Orioles"),
    111: ("BOS", "Boston", "Boston Red Sox"),
    112: ("CHC", "Chicago C", "Chicago Cubs"),
    # Reviewed against Kalshi COLCWS rules and official MLB game 824542, 2026-09-27.
    145: ("CWS", "CHW", "Chicago W", "Chicago WS", "Chicago White Sox"),
    113: ("CIN", "Cincinnati", "Cincinnati Reds"),
    114: ("CLE", "Cleveland", "Cleveland Guardians"),
    115: ("COL", "Colorado", "Colorado Rockies"),
    116: ("DET", "Detroit", "Detroit Tigers"),
    117: ("HOU", "Houston", "Houston Astros"),
    118: ("KC", "KCR", "Kansas City", "Kansas City Royals"),
    108: ("LAA", "Los Angeles A", "Los Angeles Angels"),
    119: ("LAD", "Los Angeles D", "Los Angeles Dodgers"),
    146: ("MIA", "Miami", "Miami Marlins"),
    158: ("MIL", "Milwaukee", "Milwaukee Brewers"),
    142: ("MIN", "Minnesota", "Minnesota Twins"),
    121: ("NYM", "New York M", "New York Mets"),
    147: ("NYY", "New York Y", "New York Yankees"),
    133: ("ATH", "OAK", "A's", "Athletics", "Oakland Athletics"),
    143: ("PHI", "Philadelphia", "Philadelphia Phillies"),
    134: ("PIT", "Pittsburgh", "Pittsburgh Pirates"),
    135: ("SD", "SDP", "San Diego", "San Diego Padres"),
    136: ("SEA", "Seattle", "Seattle Mariners"),
    137: ("SF", "SFG", "San Francisco", "San Francisco Giants"),
    138: ("STL", "St. Louis", "St Louis", "St. Louis Cardinals"),
    139: ("TB", "TBR", "Tampa Bay", "Tampa Bay Rays"),
    140: ("TEX", "Texas", "Texas Rangers"),
    141: ("TOR", "Toronto", "Toronto Blue Jays"),
    120: ("WSH", "WAS", "Washington", "Washington Nationals"),
}
ALIASES = {alias: team_id for team_id, aliases in TEAMS.items() for alias in aliases}
RULE = re.compile(
    r"If (.+) wins the (.+) vs (.+) professional baseball game originally scheduled for "
    r"([A-Z][a-z]{2} \d{1,2}, \d{4}) at (\d{1,2}:\d{2} [AP]M) (EDT|EST), "
    r"then the market resolves to Yes\."
)
# Rule v2 (decision D-002): the date-only wording Kalshi used for postseason games from
# about 2026-10-09. It names no time, so the start comes from the official schedule and
# is corroborated by the event ticker's time code (TICKER, interpretation ticker-v1).
RULE_V2 = re.compile(
    r"If (.+) wins the (.+) vs (.+) professional baseball game originally scheduled for "
    r"([A-Z][a-z]{2} \d{1,2}, \d{4}), then the market resolves to Yes\."
)
TICKER = re.compile(r"KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})(\d{4})([A-Z]+?)(G[12])?")
TICKER_INTERPRETATION = "ticker-v1"
# Team codes usable in event tickers: the reviewed upper-case aliases of each team.
CODES = {
    team_id: tuple(a for a in aliases if a.isalpha() and a.isupper() and 2 <= len(a) <= 3)
    for team_id, aliases in TEAMS.items()
}


@dataclass
class Eligibility:
    eligible: bool
    reason: str
    game_id: int | None = None
    cutoff: str | None = None
    scheduled_start: str | None = None
    team_id: int | None = None
    mapping_version: int = 1

    def to_dict(self):
        return asdict(self)


def ticker_identity(event_ticker):
    """Interpretation ticker-v1 of a KXMLBGAME event ticker, or None if it does not parse.

    `KXMLBGAME-26OCT192008CLETB` → 2026-10-19 20:08 America/New_York, team codes "CLETB",
    no game number. Kalshi does not document this structure. It is corroboration only:
    in the archive every timed rule's stated time equals its ticker time code (D-002), and
    every rule that uses it fails closed when it does not parse or disagrees.
    """
    match = TICKER.fullmatch(event_ticker) if isinstance(event_ticker, str) else None
    if not match:
        return None
    yy, month, day, clock, teams, game = match.groups()
    try:
        moment = datetime.strptime(
            f"20{yy} {month.title()} {day} {clock}", "%Y %b %d %H%M"
        ).replace(tzinfo=ZoneInfo("America/New_York"))
    except ValueError:
        return None
    return {
        "start": moment,
        "teams": teams,
        "game_number": int(game[1]) if game else None,
        "interpretation": TICKER_INTERPRETATION,
    }


def ticker_teams_match(code, away_id, home_id):
    """The ticker's concatenated codes name exactly these teams, away first."""
    return any(code == a + h for a in CODES.get(away_id, ()) for h in CODES.get(home_id, ()))


def eligible(market, event, event_markets, schedule, schedule_at, now, config):
    def reject(reason):
        return Eligibility(False, reason)

    if (
        not 0
        <= (now - datetime.fromisoformat(schedule_at)).total_seconds()
        <= config.schedule_max_age
    ):
        return reject("schedule_stale")
    metadata = event.get("product_metadata", {})
    if (
        not isinstance(metadata, dict)
        or event.get("series_ticker") != "KXMLBGAME"
        or metadata.get("competition") != "Pro Baseball"
        or metadata.get("competition_scope") != "Game"
    ):
        return reject("unsupported_event_scope")
    if (
        market.get("event_ticker") != event.get("event_ticker")
        or market.get("market_type") != "binary"
    ):
        return reject("unsupported_market_scope")
    if market.get("status") not in ("active", "open"):
        return reject("market_not_open")
    try:
        if decimal_value(market.get("notional_value_dollars"), price=True) != 1:
            return reject("unsupported_notional")
    except ValueError:
        return reject("unknown_notional")
    match = RULE.fullmatch(market.get("rules_primary", ""))
    if not match:
        dated = RULE_V2.fullmatch(market.get("rules_primary", "") or "")
        if dated and "rule-v2" in getattr(config, "eligibility_rules", ()):
            return _eligible_v2(market, event, event_markets, schedule, now, config, dated)
        return reject("unrecognized_full_game_rule")
    winner, away, home, day, clock, zone = match.groups()
    if any(name not in ALIASES for name in (winner, away, home)):
        return reject("unknown_team_alias")
    winner_id, away_id, home_id = (ALIASES[x] for x in (winner, away, home))
    if away_id == home_id or winner_id not in (away_id, home_id):
        return reject("invalid_participants")
    if len(event_markets) != 2 or {ALIASES.get(m.get("yes_sub_title")) for m in event_markets} != {
        away_id,
        home_id,
    }:
        return reject("ambiguous_contract_family")
    if ALIASES.get(market.get("yes_sub_title")) != winner_id:
        return reject("winner_label_mismatch")
    try:
        original = datetime.strptime(day + " " + clock, "%b %d, %Y %I:%M %p").replace(
            tzinfo=ZoneInfo("America/New_York")
        )
    except ValueError:
        return reject("invalid_rule_time")
    if original.tzname() != zone:
        return reject("rule_timezone_mismatch")
    suffix = re.search(r"G([12])$", event["event_ticker"])
    game_number = int(suffix[1]) if suffix else None
    candidates = []
    for date in schedule.get("dates", []):
        for game in date.get("games", []):
            try:
                teams = game["teams"]
                if (teams["away"]["team"]["id"], teams["home"]["team"]["id"]) != (away_id, home_id):
                    continue
                if game.get("officialDate") != original.date().isoformat():
                    continue
                if game_number is not None and game.get("gameNumber") != game_number:
                    continue
                candidates.append(game)
            except (KeyError, TypeError):
                continue
    if len(candidates) != 1:
        return reject("unmapped_game" if not candidates else "ambiguous_game")
    game = candidates[0]
    if game.get("gameType") not in {"R", "F", "D", "L", "W"}:
        return reject("unsupported_game_type")
    if game.get("doubleHeader", "N") != "N" and game_number is None:
        return reject("doubleheader_requires_game_number")
    if any(
        game.get(k)
        for k in (
            "rescheduledFrom",
            "rescheduledFromDate",
            "rescheduleDate",
            "rescheduleGameDate",
            "resumedFrom",
            "resumeDate",
            "resumeGameDate",
        )
    ):
        return reject("rescheduled_game")
    status = game.get("status", {})
    if not isinstance(status, dict):
        return reject("not_scheduled_pregame")
    if status.get("startTimeTBD") is not False:
        return reject("start_time_unknown")
    if status.get("abstractGameState") != "Preview" or status.get("detailedState") not in {
        "Scheduled",
        "Pre-Game",
    }:
        return reject("not_scheduled_pregame")
    try:
        raw_start = game.get("gameDate")
        if not isinstance(raw_start, str):
            return reject("start_time_unknown")
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        if start.utcoffset() is None:
            return reject("start_time_unknown")
    except (KeyError, ValueError, TypeError):
        return reject("start_time_unknown")
    if start != original:
        return reject("schedule_rule_time_mismatch")
    cutoff = start - timedelta(seconds=config.pregame_buffer)
    result = Eligibility(
        now < cutoff,
        "eligible" if now < cutoff else "past_pregame_cutoff",
        game["gamePk"],
        cutoff.isoformat(),
        start.isoformat(),
        winner_id,
    )
    return result


def _eligible_v2(market, event, event_markets, schedule, now, config, match):
    """Rule v2 (decision D-002) for a date-only rule; every other check is rule v1's.

    Identity: the rule's date and exact participants, corroborated by the event ticker
    (same date, same teams away-first, no game number), must identify exactly one
    official game between the two teams on that official date, in either home/away
    order. The start is only the fresh official schedule's gameDate, and it must equal
    the ticker's time (a revised start fails closed, as rule v1's rule-time check does).
    Close, expiration and occurrence times are never used. Unknowns fail closed.
    """

    def reject(reason):
        return Eligibility(False, reason)

    winner, away, home, day = match.groups()
    if any(name not in ALIASES for name in (winner, away, home)):
        return reject("unknown_team_alias")
    winner_id, away_id, home_id = (ALIASES[x] for x in (winner, away, home))
    if away_id == home_id or winner_id not in (away_id, home_id):
        return reject("invalid_participants")
    if len(event_markets) != 2 or {ALIASES.get(m.get("yes_sub_title")) for m in event_markets} != {
        away_id,
        home_id,
    }:
        return reject("ambiguous_contract_family")
    if ALIASES.get(market.get("yes_sub_title")) != winner_id:
        return reject("winner_label_mismatch")
    try:
        original_date = datetime.strptime(day, "%b %d, %Y").date()
    except ValueError:
        return reject("invalid_rule_time")
    ticker = ticker_identity(event.get("event_ticker"))
    if ticker is None:
        return reject("ticker_unparsed")
    if (
        ticker["start"].date() != original_date
        or not ticker_teams_match(ticker["teams"], away_id, home_id)
        or ticker["game_number"] is not None
    ):
        return reject("ticker_identity_mismatch")
    candidates, reversed_pair = [], []
    for date in schedule.get("dates", []):
        for game in date.get("games", []):
            try:
                teams = game["teams"]
                pair = (teams["away"]["team"]["id"], teams["home"]["team"]["id"])
                if game.get("officialDate") != original_date.isoformat():
                    continue
                if pair == (away_id, home_id):
                    candidates.append(game)
                elif pair == (home_id, away_id):
                    reversed_pair.append(game)
            except (KeyError, TypeError):
                continue
    if len({g.get("gamePk") for g in candidates + reversed_pair}) > 1:
        return reject("ambiguous_game")
    if not candidates:
        return reject("home_away_reversed" if reversed_pair else "unmapped_game")
    game = candidates[0]
    if game.get("gameType") not in {"R", "F", "D", "L", "W"}:
        return reject("unsupported_game_type")
    if game.get("doubleHeader", "N") != "N":
        return reject("doubleheader_requires_game_number")
    if any(
        game.get(k)
        for k in (
            "rescheduledFrom",
            "rescheduledFromDate",
            "rescheduleDate",
            "rescheduleGameDate",
            "resumedFrom",
            "resumeDate",
            "resumeGameDate",
        )
    ):
        return reject("rescheduled_game")
    status = game.get("status", {})
    if not isinstance(status, dict):
        return reject("not_scheduled_pregame")
    if status.get("startTimeTBD") is not False:
        return reject("start_time_unknown")
    if status.get("abstractGameState") != "Preview" or status.get("detailedState") not in {
        "Scheduled",
        "Pre-Game",
    }:
        return reject("not_scheduled_pregame")
    try:
        raw_start = game.get("gameDate")
        if not isinstance(raw_start, str):
            return reject("start_time_unknown")
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        if start.utcoffset() is None:
            return reject("start_time_unknown")
    except (KeyError, ValueError, TypeError):
        return reject("start_time_unknown")
    if start != ticker["start"]:
        return reject("ticker_start_mismatch")
    cutoff = start - timedelta(seconds=config.pregame_buffer)
    return Eligibility(
        now < cutoff,
        "eligible" if now < cutoff else "past_pregame_cutoff",
        game["gamePk"],
        cutoff.isoformat(),
        start.isoformat(),
        winner_id,
        mapping_version=2,
    )
