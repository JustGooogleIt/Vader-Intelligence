"""A5 increment 1 (D-020): NFL/NBA identity, schedule parsing and eligibility.

All rows are synthetic, shaped like the real nflverse `games.csv` and sportsdataverse
`nba_schedule_<season>.csv` columns; no provider payload is stored in the repository.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from vader_intelligence.leagues import LEAGUES, NBA, NFL
from vader_intelligence.leagues.eligibility import eligible
from vader_intelligence.leagues.identity import Candidate, Rejection, candidate, parse_event_ticker
from vader_intelligence.leagues.schedules import (
    parse_espn_nba,
    parse_nflverse,
    parse_nflverse_timestamp,
)

UTC = timezone.utc


def market(league, event, code, *, status="active", sport=None, when="Nov 30, 2026"):
    team = league.by_code(code)
    return {
        "ticker": f"{event}-{code}",
        "custom_strike": {league.strike_key: team.uuid},
        "status": status,
        "rules_primary": f"If {team.label} wins the A vs B {sport or league.sport_phrase} game "
        f"originally scheduled for {when}, then the market resolves to Yes.",
    }


def nfl_row(**extra):
    row = {
        "game_id": "2026_12_CAR_TB",
        "game_type": "REG",
        "gameday": "2026-11-30",
        "gametime": "20:15",
        "away_team": "CAR",
        "home_team": "TB",
        "location": "Home",
        "result": "",
    }
    return row | extra


def nba_row(**extra):
    row = {
        "id": "401910001",
        "start_date": "2026-11-28T03:00Z",  # 22:00 ET on Nov 27
        "date": "2026-11-28T03:00Z",
        "time_valid": "true",
        "away_abbreviation": "DAL",
        "home_abbreviation": "PHX",
        "season_type": "2",
        "neutral_site": "false",
        "status_type_name": "STATUS_SCHEDULED",
    }
    return row | extra


# Identity -------------------------------------------------------------------------------


def test_team_tables_are_complete_and_one_to_one():
    for league, count in ((NFL, 32), (NBA, 30)):
        assert len(league.teams) == count
        for field in ("code", "uuid", "source"):
            values = [getattr(t, field) for t in league.teams]
            assert len(set(values)) == count, (league.key, field)
    assert NFL.by_code("JAC").source == "JAX" and NFL.by_code("LAR").source == "LA"
    assert NBA.by_code("GSW").source == "GS" and NBA.by_code("UTA").source == "UTAH"
    assert NBA.by_code("LIO") is None  # exhibition opponent: never a candidate
    assert set(LEAGUES) == {"nfl", "nba"}


@pytest.mark.parametrize(
    "league, ticker, expected",
    [
        (NFL, "KXNFLGAME-26NOV30CARTB", (date(2026, 11, 30), "CAR", "TB")),
        (NFL, "KXNFLGAME-26DEC06LACKC", (date(2026, 12, 6), "LAC", "KC")),
        (NBA, "KXNBAGAME-26NOV27DALPHX", (date(2026, 11, 27), "DAL", "PHX")),
    ],
)
def test_event_tickers_split_into_known_codes(league, ticker, expected):
    day, away, home = parse_event_ticker(league, ticker)
    assert (day, away.code, home.code) == expected


@pytest.mark.parametrize(
    "league, ticker, reason",
    [
        (NFL, "KXNBAGAME-26NOV27DALPHX", "wrong_series"),
        (NFL, "KXNFLGAME-26XYZ30CARTB", "unparseable_ticker"),
        (NFL, "KXNFLGAME-26FEB30CARTB", "unparseable_ticker"),
        (NBA, "KXNBAGAME-26OCT12LIOPOR", "unknown_team"),
        (NFL, "KXNFLGAME-26NOV30TBTB", "unknown_team"),
    ],
)
def test_bad_tickers_are_rejected(league, ticker, reason):
    assert parse_event_ticker(league, ticker) == Rejection(reason)


def test_a_candidate_needs_both_markets_uuids_and_matching_rules():
    event = "KXNFLGAME-26NOV30CARTB"
    good = [market(NFL, event, "CAR"), market(NFL, event, "TB")]
    found = candidate(NFL, event, good)
    assert isinstance(found, Candidate) and found.markets[0] == ("CAR", f"{event}-CAR")
    assert candidate(NFL, event, good[:1]) == Rejection("ambiguous_contract_family")
    wrong_uuid = [good[0], {**good[1], "custom_strike": {"football_team": NFL.teams[0].uuid}}]
    assert candidate(NFL, event, wrong_uuid) == Rejection("team_identity_mismatch")
    closed = [good[0], market(NFL, event, "TB", status="closed")]
    assert candidate(NFL, event, closed) == Rejection("market_not_active")
    other_date = [good[0], market(NFL, event, "TB", when="Dec 1, 2026")]
    assert candidate(NFL, event, other_date) == Rejection("rule_date_mismatch")
    other_sport = [good[0], market(NFL, event, "TB", sport="Pro Basketball")]
    assert candidate(NFL, event, other_sport) == Rejection("unrecognized_rule")


# Schedules ------------------------------------------------------------------------------


def test_nflverse_rows_use_eastern_time_and_mark_results_final():
    games, rejected = parse_nflverse(
        [nfl_row(), nfl_row(game_id="x", result="7"), nfl_row(gameday="bad")]
    )
    assert [r["reason"] for r in rejected] == ["malformed_row"]
    first, played = games
    assert first.start == datetime(2026, 12, 1, 1, 15, tzinfo=UTC)  # 20:15 EST
    assert first.et_date == date(2026, 11, 30) and first.state == "scheduled"
    assert played.state == "final"
    assert parse_nflverse([nfl_row(location="Neutral")])[0][0].neutral is True
    assert parse_nflverse([nfl_row(game_type="WC")])[0][0].season_type == "POST"
    assert parse_nflverse([nfl_row(gametime="")])[0][0].start is None


def test_espn_nba_rows_use_utc_and_map_status():
    (game,), _ = parse_espn_nba([nba_row()])
    assert game.et_date == date(2026, 11, 27) and game.start.hour == 3
    (postponed,), _ = parse_espn_nba([nba_row(status_type_name="STATUS_POSTPONED")])
    assert postponed.state == "postponed"
    (unknown_time,), _ = parse_espn_nba([nba_row(time_valid="false")])
    assert unknown_time.start is None
    (cup,), _ = parse_espn_nba([nba_row(away_abbreviation="TBD", home_abbreviation="TBD")])
    assert cup.away == "TBD"  # never matches a Kalshi team
    assert parse_espn_nba([nba_row(start_date="2026-11-28T03:00")])[1]  # naive: rejected


def test_nflverse_timestamp_labels():
    assert parse_nflverse_timestamp("2026-11-23 05:06:22 EST") == datetime(
        2026, 11, 23, 10, 6, 22, tzinfo=UTC
    )
    assert parse_nflverse_timestamp("2026-07-01 05:00:00 EDT").hour == 9
    with pytest.raises(ValueError):
        parse_nflverse_timestamp("2026-11-23 05:06:22 PST")


# Eligibility ----------------------------------------------------------------------------


def nfl_candidate():
    event = "KXNFLGAME-26NOV30CARTB"
    return candidate(NFL, event, [market(NFL, event, "CAR"), market(NFL, event, "TB")])


def check(league, cand, games, now, **extra):
    times = {
        "content_time": now - timedelta(minutes=20),
        "retrieved_at": now - timedelta(seconds=30),
    }
    return eligible(league, cand, games, now=now, **(times | extra))


def test_an_ordinary_nfl_game_is_eligible_until_two_minutes_before_kickoff():
    games, _ = parse_nflverse([nfl_row()])
    kickoff = datetime(2026, 12, 1, 1, 15, tzinfo=UTC)
    ok = check(NFL, nfl_candidate(), games, kickoff - timedelta(hours=1))
    assert ok.eligible and ok.game_id == "2026_12_CAR_TB" and ok.neutral is False
    late = check(NFL, nfl_candidate(), games, kickoff - timedelta(seconds=120))
    assert late.reason == "past_pregame_cutoff"


def test_source_abbreviations_differ_from_kalshi_codes():
    event = "KXNFLGAME-26DEC06JACLAR"
    cand = candidate(
        NFL,
        event,
        [
            market(NFL, event, "JAC", when="Dec 6, 2026"),
            market(NFL, event, "LAR", when="Dec 6, 2026"),
        ],
    )
    games, _ = parse_nflverse(
        [
            nfl_row(
                game_id="g", gameday="2026-12-06", gametime="16:05", away_team="JAX", home_team="LA"
            )
        ]
    )
    assert check(NFL, cand, games, datetime(2026, 12, 6, 18, tzinfo=UTC)).eligible


@pytest.mark.parametrize(
    "rows, reason",
    [
        ([], "unmapped_game"),
        ([nfl_row(), nfl_row(game_id="dup")], "ambiguous_game"),
        ([nfl_row(gametime="")], "start_time_unknown"),
        ([nfl_row(result="3")], "not_scheduled_pregame"),
        ([nfl_row(game_type="PRE")], "unsupported_season_type"),
    ],
)
def test_nfl_refusals(rows, reason):
    games, _ = parse_nflverse(rows)
    now = datetime(2026, 11, 30, 20, tzinfo=UTC)
    assert check(NFL, nfl_candidate(), games, now).reason == reason


def test_stale_content_or_retrieval_is_refused_even_when_otherwise_eligible():
    games, _ = parse_nflverse([nfl_row()])
    now = datetime(2026, 11, 30, 20, tzinfo=UTC)
    stale = check(
        NFL, nfl_candidate(), games, now, content_time=now - timedelta(hours=2, seconds=1)
    )
    assert stale.reason == "schedule_content_stale"
    future = check(NFL, nfl_candidate(), games, now, content_time=now + timedelta(seconds=1))
    assert future.reason == "schedule_content_stale"
    old_fetch = check(NFL, nfl_candidate(), games, now, retrieved_at=now - timedelta(seconds=121))
    assert old_fetch.reason == "schedule_retrieval_stale"
    edge = check(NFL, nfl_candidate(), games, now, content_time=now - timedelta(hours=2))
    assert edge.eligible


def test_nba_postponement_unknown_time_and_daily_content_bound():
    event = "KXNBAGAME-26NOV27DALPHX"
    cand = candidate(
        NBA,
        event,
        [
            market(NBA, event, "DAL", when="Nov 27, 2026"),
            market(NBA, event, "PHX", when="Nov 27, 2026"),
        ],
    )
    now = datetime(2026, 11, 27, 22, tzinfo=UTC)
    ok, _ = parse_espn_nba([nba_row()])
    assert check(NBA, cand, ok, now, content_time=now - timedelta(hours=30)).eligible
    assert check(NBA, cand, ok, now, content_time=now - timedelta(hours=37)).reason == (
        "schedule_content_stale"
    )
    postponed, _ = parse_espn_nba([nba_row(status_type_name="STATUS_POSTPONED")])
    assert check(NBA, cand, postponed, now).reason == "not_scheduled_pregame"
    unknown, _ = parse_espn_nba([nba_row(time_valid="false")])
    assert check(NBA, cand, unknown, now).reason == "start_time_unknown"


def test_a_makeup_game_matches_only_its_own_date():
    """Postponed originals keep their ticker; the makeup gets a new ticker and date."""
    games, _ = parse_espn_nba(
        [
            nba_row(id="orig", status_type_name="STATUS_POSTPONED"),
            nba_row(id="makeup", start_date="2026-12-15T01:00Z", date="2026-12-15T01:00Z"),
        ]
    )
    event = "KXNBAGAME-26DEC14DALPHX"
    cand = candidate(
        NBA,
        event,
        [
            market(NBA, event, "DAL", when="Dec 14, 2026"),
            market(NBA, event, "PHX", when="Dec 14, 2026"),
        ],
    )
    result = check(NBA, cand, games, datetime(2026, 12, 14, 20, tzinfo=UTC))
    assert result.eligible and result.game_id == "makeup"
