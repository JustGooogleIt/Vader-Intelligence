"""Parsers for the two open-data schedule files chosen in D-020.

Both turn rows (dicts from `csv.DictReader`) into `ScheduleGame`s. Malformed rows are
returned as rejected entries rather than silently dropped, so a caller can report them.

- nflverse `games.csv`: `gameday` and `gametime` are US Eastern; `game_type` REG, WC, DIV,
  CON or SB (no preseason); `location` Home or Neutral; a recorded `result` means the game
  is over. There is no status column, so an unplayed game is `scheduled`.
- sportsdataverse `nba_schedule_<season>.csv` (ESPN): `start_date` is UTC; `time_valid`
  says whether the time is known; `status_type_name` gives the status; `season_type`
  1 preseason, 2 regular, 3 postseason, 5 play-in.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
NFL_TYPES = {"REG": "REG", "WC": "POST", "DIV": "POST", "CON": "POST", "SB": "POST"}
NBA_TYPES = {"1": "PRE", "2": "REG", "3": "POST", "5": "POST"}
NBA_STATES = {
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_IN_PROGRESS": "live",
    "STATUS_HALFTIME": "live",
    "STATUS_END_PERIOD": "live",
    "STATUS_FINAL": "final",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "canceled",
    "STATUS_SUSPENDED": "suspended",
    "STATUS_DELAYED": "delayed",
}


@dataclass(frozen=True)
class ScheduleGame:
    source: str
    game_id: str
    et_date: date
    start: datetime | None  # aware UTC; None when the source does not know the time
    away: str  # source abbreviation
    home: str
    season_type: str  # PRE, REG, POST or OTHER
    neutral: bool
    state: str  # scheduled, live, final, postponed, canceled, suspended, delayed, unknown


def _row_error(row, reason):
    return {"reason": reason, "game_id": str((row or {}).get("game_id") or (row or {}).get("id"))}


def parse_nflverse(rows):
    games, rejected = [], []
    for row in rows:
        try:
            day = date.fromisoformat(row["gameday"])
            clock = row.get("gametime") or ""
            start = None
            if re.fullmatch(r"\d{2}:\d{2}", clock):
                hour, minute = map(int, clock.split(":"))
                start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=EASTERN)
                start = start.astimezone(timezone.utc)
            games.append(
                ScheduleGame(
                    source="nflverse-schedules",
                    game_id=row["game_id"],
                    et_date=day,
                    start=start,
                    away=row["away_team"],
                    home=row["home_team"],
                    season_type=NFL_TYPES.get(row.get("game_type"), "OTHER"),
                    neutral=row.get("location") == "Neutral",
                    state="final" if (row.get("result") or "").strip() else "scheduled",
                )
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            rejected.append(_row_error(row, "malformed_row"))
    return games, rejected


def parse_espn_nba(rows):
    games, rejected = [], []
    for row in rows:
        try:
            raw = row["start_date"] or row["date"]
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if moment.utcoffset() is None:
                raise ValueError("naive start")
            moment = moment.astimezone(timezone.utc)
            known = row.get("time_valid") == "true"
            games.append(
                ScheduleGame(
                    source="espn-nba-schedules",
                    game_id=str(row["id"]),
                    et_date=moment.astimezone(EASTERN).date(),
                    start=moment if known else None,
                    away=row["away_abbreviation"],
                    home=row["home_abbreviation"],
                    season_type=NBA_TYPES.get(str(row.get("season_type")), "OTHER"),
                    neutral=row.get("neutral_site") == "true",
                    state=NBA_STATES.get(row.get("status_type_name"), "unknown"),
                )
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            rejected.append(_row_error(row, "malformed_row"))
    return games, rejected


def parse_nflverse_timestamp(text):
    """nflverse `timestamp.json` `last_updated`, e.g. "2026-11-23 05:06:22 EST" → aware
    UTC. Only EST and EDT labels are accepted."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (EST|EDT)", (text or "").strip())
    if not match:
        raise ValueError("unrecognized nflverse timestamp")
    offset = timedelta(hours=-5 if match[2] == "EST" else -4)
    naive = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
