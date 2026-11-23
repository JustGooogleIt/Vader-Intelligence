"""NFL and NBA verticals (A5, D-020), increment 1: pure identity, schedule parsing and
eligibility. No network, clock, storage or configuration here; callers pass every input,
including `now`. Plan: `docs/specs/nfl-nba-v1-plan.md`.

A Kalshi candidate is identified by its event ticker (US Eastern date, away code, home
code) and confirmed by both markets' team UUIDs and the rule text. The schedule source
supplies the official start, season type and status; Kalshi time fields are never used
for the start. Settlement stays Kalshi's result.
"""

from dataclasses import dataclass
from datetime import timedelta

from .teams import NBA_TEAMS, NFL_TEAMS, Team


@dataclass(frozen=True)
class League:
    key: str
    series: str
    strike_key: str  # custom_strike field holding the team UUID
    sport_phrase: str  # as written in rules_primary
    protocol: str
    rule_version: str
    source: str
    content_max_age: timedelta  # how old the schedule source's own content may be
    teams: tuple

    def by_code(self, code):
        return next((t for t in self.teams if t.code == code), None)


NFL = League(
    key="nfl",
    series="KXNFLGAME",
    strike_key="football_team",
    sport_phrase="Pro Football",
    protocol="nfl-t60-v1",
    rule_version="nfl-rule-v1",
    source="nflverse-schedules",
    content_max_age=timedelta(hours=2),
    teams=NFL_TEAMS,
)
NBA = League(
    key="nba",
    series="KXNBAGAME",
    strike_key="basketball_team",
    sport_phrase="Pro Basketball",
    protocol="nba-t60-v1",
    rule_version="nba-rule-v1",
    source="espn-nba-schedules",
    content_max_age=timedelta(hours=36),
    teams=NBA_TEAMS,
)
LEAGUES = {league.key: league for league in (NFL, NBA)}

__all__ = ["LEAGUES", "NBA", "NFL", "League", "Team"]
