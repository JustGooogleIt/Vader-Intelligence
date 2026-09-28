import copy
import json
from datetime import datetime, timedelta

import pytest

from vader_intelligence.normalize import book_data, decimal_value
from vader_intelligence.scope import eligible


@pytest.mark.parametrize("value", [0.5, -1, "NaN", "Infinity", "-0.1", "1e-3", " 0.5", "1.01", ""])
def test_invalid_price_is_not_coerced(value):
    with pytest.raises(ValueError):
        decimal_value(value, price=True)


def test_subcent_prices_fractional_sizes_and_same_book_complements():
    result = book_data(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4001", "1.25"], ["0.4100", "0.50"]],
                "no_dollars": [["0.5800", "7.75"]],
            }
        }
    )
    assert result["yes_ask_dollars"] == "0.4200"
    assert result["yes_spread_dollars"] == "0.0100"
    assert result["yes_ask_size_fp"] == "7.75"
    assert result["no_ask_dollars"] == "0.5900"


def test_missing_null_empty_and_zero_quantity():
    result = book_data({"orderbook_fp": {"yes_dollars": []}})
    assert result["side_state"] == {"yes": "empty", "no": "missing"}
    assert result["yes_ask_dollars"] is None
    result = book_data({"orderbook_fp": {"yes_dollars": None, "no_dollars": [["0.5000", "0.00"]]}})
    assert result["side_state"]["yes"] == "null"
    assert result["yes_ask_dollars"] is None


def test_crossed_book_is_flagged_not_clamped():
    result = book_data(
        {"orderbook_fp": {"yes_dollars": [["0.7", "1"]], "no_dollars": [["0.4", "1"]]}}
    )
    assert result["yes_spread_dollars"] == "-0.1"
    assert "crossed_book" in result["flags"]


@pytest.mark.parametrize(
    "book",
    [
        {},
        {"orderbook_fp": None},
        {"orderbook_fp": {"yes_dollars": "bad"}},
        {"orderbook_fp": {"yes_dollars": [["0.4"]]}},
        {"orderbook_fp": {"yes_dollars": [["0.4", "1"], ["0.40", "2"]]}},
    ],
)
def test_malformed_books_rejected(book):
    with pytest.raises(ValueError):
        book_data(book)


def context(fixture_data):
    event = json.loads(next(x["body"] for x in fixture_data["responses"] if x["stage"] == "event"))
    entry = next(x for x in fixture_data["responses"] if x["stage"] == "schedule")
    schedule = json.loads(entry["body"])
    return event, schedule, entry["retrieved_at"], datetime.fromisoformat(entry["retrieved_at"])


def test_verified_match(config, fixture_data):
    event, schedule, at, now = context(fixture_data)
    check = eligible(
        event["markets"][0], event["event"], event["markets"], schedule, at, now, config
    )
    assert check.eligible
    assert check.game_id == 824948
    assert datetime.fromisoformat(check.cutoff).hour == 19


@pytest.mark.parametrize(
    "case,reason",
    [
        ("unknown_team", "unknown_team_alias"),
        ("tbd", "start_time_unknown"),
        ("postponed", "not_scheduled_pregame"),
        ("in_game", "not_scheduled_pregame"),
        ("rescheduled", "rescheduled_game"),
        ("time_mismatch", "schedule_rule_time_mismatch"),
        ("doubleheader", "doubleheader_requires_game_number"),
        ("ambiguous", "ambiguous_game"),
        ("prop", "unrecognized_full_game_rule"),
        ("wrong_league", "unsupported_event_scope"),
        ("closed", "market_not_open"),
        ("nonbinary", "unsupported_market_scope"),
    ],
)
def test_ineligible_cases_fail_closed(config, fixture_data, case, reason):
    event, schedule, at, now = context(fixture_data)
    m = event["markets"][0]
    game = schedule["dates"][0]["games"][0]
    if case == "unknown_team":
        m["rules_primary"] = m["rules_primary"].replace("Houston", "Unknown")
    elif case == "tbd":
        game["status"]["startTimeTBD"] = True
    elif case == "postponed":
        game["status"]["detailedState"] = "Postponed"
    elif case == "in_game":
        game["status"]["abstractGameState"] = "Live"
    elif case == "rescheduled":
        game["rescheduledFrom"] = "2026-09-26"
    elif case == "time_mismatch":
        game["gameDate"] = "2026-09-27T20:05:00Z"
    elif case == "doubleheader":
        game["doubleHeader"] = "Y"
    elif case == "ambiguous":
        schedule["dates"][0]["games"].append(copy.deepcopy(game))
    elif case == "prop":
        m["rules_primary"] = m["rules_primary"].replace(
            "professional baseball game", "first five innings"
        )
    elif case == "wrong_league":
        event["event"]["product_metadata"]["competition"] = "College Baseball"
    elif case == "closed":
        m["status"] = "finalized"
    elif case == "nonbinary":
        m["market_type"] = "scalar"
    assert eligible(m, event["event"], event["markets"], schedule, at, now, config).reason == reason


def test_doubleheader_requires_explicit_game_number(config, fixture_data):
    event, schedule, at, now = context(fixture_data)
    event["event"]["event_ticker"] += "G1"
    for market in event["markets"]:
        market["event_ticker"] += "G1"
    schedule["dates"][0]["games"][0]["doubleHeader"] = "Y"
    other = copy.deepcopy(schedule["dates"][0]["games"][0])
    other["gamePk"], other["gameNumber"] = 99, 2
    schedule["dates"][0]["games"].append(other)
    assert eligible(
        event["markets"][0], event["event"], event["markets"], schedule, at, now, config
    ).eligible


@pytest.mark.parametrize("offset,allowed", [(-0.001, True), (0, False), (1, False)])
def test_pregame_cutoff_boundary(config, fixture_data, offset, allowed):
    event, schedule, _, _ = context(fixture_data)
    now = datetime.fromisoformat("2026-09-27T19:03:00+00:00") + timedelta(seconds=offset)
    result = eligible(
        event["markets"][0],
        event["event"],
        event["markets"],
        schedule,
        now.isoformat(),
        now,
        config,
    )
    assert result.eligible is allowed


@pytest.mark.parametrize("age", [-1, 120.001])
def test_schedule_clock_skew_and_staleness(config, fixture_data, age):
    event, schedule, _, now = context(fixture_data)
    at = (now - timedelta(seconds=age)).isoformat()
    assert (
        eligible(
            event["markets"][0], event["event"], event["markets"], schedule, at, now, config
        ).reason
        == "schedule_stale"
    )
