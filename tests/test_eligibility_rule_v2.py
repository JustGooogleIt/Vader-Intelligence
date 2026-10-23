"""Eligibility/mapping rule v2 for date-only postseason rules (decision D-002).

The rule wording below follows the date-only form archived from live KXMLBGAME markets
from about 2026-10-09 ("...game originally scheduled for Oct 19, 2026, then the market
resolves to Yes."). Markets, schedules and books are the shipped synthetic fixture,
rewritten into that wording; nothing here is live evidence.
"""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from vader_intelligence.config import DEFAULT_RULES, Config
from vader_intelligence.forecast.references import digest
from vader_intelligence.mapping import original_date, resolve, rule_identity
from vader_intelligence.scope import eligible, ticker_identity, ticker_teams_match

V2 = ("rule-v1", "rule-v2")
DATED = (
    "If {winner} wins the Houston vs A's professional baseball game originally scheduled"
    " for Sep 27, 2026, then the market resolves to Yes."
)


def dated(fixture_data):
    """The fixture with its two contracts' rules rewritten to the date-only wording."""
    data = copy.deepcopy(fixture_data)
    for entry in data["responses"]:
        if entry["stage"] == "event":
            body = json.loads(entry["body"])
            for market in body["markets"]:
                market["rules_primary"] = DATED.format(winner=market["yes_sub_title"])
            entry["body"] = json.dumps(body)
    return data


def context(fixture_data):
    event = json.loads(next(x["body"] for x in fixture_data["responses"] if x["stage"] == "event"))
    entry = next(x for x in fixture_data["responses"] if x["stage"] == "schedule")
    schedule = json.loads(entry["body"])
    return event, schedule, entry["retrieved_at"], datetime.fromisoformat(entry["retrieved_at"])


def check(fixture_data, config, *, event_change=None, game_change=None, extra_games=(), now=None):
    event, schedule, at, moment = context(dated(fixture_data))
    if event_change:
        event["event"].update(event_change)
        for market in event["markets"]:
            market.update({k: v for k, v in event_change.items() if k == "event_ticker"})
    game = schedule["dates"][0]["games"][0]
    if game_change:
        game.update(game_change)
    schedule["dates"][0]["games"].extend(extra_games)
    return eligible(
        event["markets"][0],
        event["event"],
        event["markets"],
        schedule,
        at,
        now or moment,
        config,
    )


@pytest.fixture
def config_v2(tmp_path):
    return Config(database=str(tmp_path / "v2.sqlite3"), min_free_bytes=1, eligibility_rules=V2)


def test_default_rules_keep_rejecting_date_only_wording(config, fixture_data):
    """The live finding of 2026-10-14 stays true under the default configuration."""
    result = check(fixture_data, config)
    assert not result.eligible and result.reason == "unrecognized_full_game_rule"


def test_rule_v2_establishes_identity_and_takes_the_start_from_the_official_schedule(
    config_v2, fixture_data
):
    result = check(fixture_data, config_v2)
    assert result.eligible and result.reason == "eligible"
    assert result.game_id == 824948 and result.team_id == 117
    assert result.scheduled_start == "2026-09-27T19:05:00+00:00"
    assert result.cutoff == "2026-09-27T19:03:00+00:00"
    assert result.mapping_version == 2


def test_timed_rules_are_unchanged_when_rule_v2_is_enabled(config, config_v2, fixture_data):
    event, schedule, at, moment = context(fixture_data)
    args = (event["markets"][0], event["event"], event["markets"], schedule, at, moment)
    assert eligible(*args, config).to_dict() == eligible(*args, config_v2).to_dict()
    assert eligible(*args, config_v2).mapping_version == 1


def test_a_revised_start_fails_closed_and_close_times_never_substitute(config_v2, fixture_data):
    moved = "2026-09-27T19:35:00Z"
    event, schedule, at, moment = context(dated(fixture_data))
    for market in event["markets"]:
        # Even when every Kalshi time field agrees with the new official start, only
        # the ticker corroborates a start, and it still says 15:05 ET.
        market.update(close_time=moved, expected_expiration_time=moved, occurrence_datetime=moved)
    schedule["dates"][0]["games"][0]["gameDate"] = moved
    result = eligible(
        event["markets"][0], event["event"], event["markets"], schedule, at, moment, config_v2
    )
    assert not result.eligible and result.reason == "ticker_start_mismatch"


@pytest.mark.parametrize(
    "ticker,reason",
    [
        ("KXMLBGAME-26SEP27HOUATH", "ticker_unparsed"),
        ("KXMLBGAME-26SEP311505HOUATH", "ticker_unparsed"),  # no such date
        ("KXMLBGAME-26SEP271505ATHHOU", "ticker_identity_mismatch"),  # home/away swapped
        ("KXMLBGAME-26SEP271505HOUSEA", "ticker_identity_mismatch"),  # other team
        ("KXMLBGAME-26SEP281505HOUATH", "ticker_identity_mismatch"),  # other date
        ("KXMLBGAME-26SEP271505HOUATHG1", "ticker_identity_mismatch"),  # game number
    ],
)
def test_ticker_corroboration_failures_fail_closed(config_v2, fixture_data, ticker, reason):
    result = check(fixture_data, config_v2, event_change={"event_ticker": ticker})
    assert not result.eligible and result.reason == reason


def game_like(fixture_data, **changes):
    _, schedule, _, _ = context(fixture_data)
    game = copy.deepcopy(schedule["dates"][0]["games"][0])
    game.update(changes)
    return game


def test_identity_ambiguity_and_home_away_reversal(config_v2, fixture_data):
    base = context(fixture_data)[1]["dates"][0]["games"][0]
    reversed_teams = {"away": base["teams"]["home"], "home": base["teams"]["away"]}
    second = game_like(fixture_data, gamePk=900001)
    assert check(fixture_data, config_v2, extra_games=[second]).reason == "ambiguous_game"
    reverse = game_like(fixture_data, gamePk=900002, teams=reversed_teams)
    assert check(fixture_data, config_v2, extra_games=[reverse]).reason == "ambiguous_game"
    only_reversed = check(fixture_data, config_v2, game_change={"teams": reversed_teams})
    assert only_reversed.reason == "home_away_reversed"
    other_day = check(fixture_data, config_v2, game_change={"officialDate": "2026-09-28"})
    assert other_day.reason == "unmapped_game"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"doubleHeader": "Y"}, "doubleheader_requires_game_number"),
        ({"gameType": "S"}, "unsupported_game_type"),
        ({"rescheduledFromDate": "2026-09-26"}, "rescheduled_game"),
        ({"status": {"abstractGameState": "Preview", "startTimeTBD": True}}, "start_time_unknown"),
        (
            {
                "status": {
                    "abstractGameState": "Live",
                    "detailedState": "In Progress",
                    "startTimeTBD": False,
                }
            },
            "not_scheduled_pregame",
        ),
        ({"gameDate": None}, "start_time_unknown"),
    ],
)
def test_official_schedule_state_is_required(config_v2, fixture_data, change, reason):
    result = check(fixture_data, config_v2, game_change=change)
    assert not result.eligible and result.reason == reason


def test_stale_schedule_and_past_cutoff(config_v2, fixture_data):
    _, _, _, moment = context(fixture_data)
    stale = check(fixture_data, config_v2, now=moment + timedelta(seconds=121))
    assert stale.reason == "schedule_stale"
    # A game whose corroborated official start is 15:01Z has its cutoff at 14:59Z, before
    # the 15:00:05Z schedule retrieval: identity holds, eligibility does not.
    late = check(
        fixture_data,
        config_v2,
        event_change={"event_ticker": "KXMLBGAME-26SEP271101HOUATH"},
        game_change={"gameDate": "2026-09-27T15:01:00Z"},
    )
    assert not late.eligible and late.reason == "past_pregame_cutoff"
    assert late.game_id == 824948 and late.cutoff == "2026-09-27T14:59:00+00:00"


def test_every_official_start_other_than_the_ticker_time_is_refused(config_v2, fixture_data):
    """Exhaustive over each minute within ten hours of the ticker time (both sides)."""
    for minutes in range(-600, 601):
        start = datetime(2026, 9, 27, 19, 5, tzinfo=timezone.utc) + timedelta(minutes=minutes)
        result = check(
            fixture_data,
            config_v2,
            game_change={"gameDate": start.isoformat().replace("+00:00", "Z")},
        )
        if minutes == 0:
            assert result.eligible
        else:
            assert not result.eligible and result.reason == "ticker_start_mismatch", minutes


def test_ticker_interpretation_handles_daylight_saving_and_codes():
    summer = ticker_identity("KXMLBGAME-26OCT192008CLETB")
    assert summer["start"].astimezone(timezone.utc).isoformat() == "2026-10-20T00:08:00+00:00"
    winter = ticker_identity("KXMLBGAME-26NOV041900LADNYY")
    assert winter["start"].astimezone(timezone.utc).isoformat() == "2026-11-05T00:00:00+00:00"
    assert ticker_identity("KXMLBGAME-26SEP251305CHCBOSG1")["game_number"] == 1
    assert ticker_teams_match("CLETB", 114, 139) and not ticker_teams_match("TBCLE", 114, 139)
    assert ticker_teams_match("CHWCLE", 145, 114) and ticker_teams_match("CWSCLE", 145, 114)
    for bad in (None, "", "KXMLBGAME-26OCT192008", "KXNBAGAME-26OCT192008CLETB"):
        assert ticker_identity(bad) is None


# --------------------------------------------------------------------- configuration


def test_rule_set_configuration_and_cohort_hash(tmp_path):
    default = Config(database="x.sqlite3")
    assert default.eligibility_rules == DEFAULT_RULES
    # A default configuration hashes exactly like the earlier code's configuration did
    # (no rule-set field), so existing collector cohorts keep their identity.
    assert "eligibility_rules" not in default.provenance()
    legacy_fields = {k: v for k, v in default.provenance().items() if k != "eligibility_rules"}
    assert digest(default.provenance()) == digest(legacy_fields)
    enabled = Config(database="x.sqlite3", eligibility_rules=["rule-v2", "rule-v1"])
    assert enabled.eligibility_rules == V2
    assert enabled.provenance()["eligibility_rules"] == list(V2)
    assert digest(enabled.provenance()) != digest(default.provenance())
    assert Config.rules_of(enabled.provenance()) == V2
    assert Config.rules_of(default.provenance()) == DEFAULT_RULES
    for bad in (("rule-v2",), ("rule-v1", "rule-v1"), ("rule-v1", "rule-v9"), "rule-v1", ()):
        with pytest.raises(ValueError, match="eligibility_rules"):
            Config(database="x.sqlite3", eligibility_rules=bad)
    toml = tmp_path / "collector.toml"
    toml.write_text('[collector]\neligibility_rules = ["rule-v1", "rule-v2"]\n')
    assert Config.load(str(toml)).eligibility_rules == V2


# ------------------------------------------------------------------------- mapping


def mapping_inputs(fixture_data, **game_changes):
    event, schedule, _, _ = context(dated(fixture_data))
    market = copy.deepcopy(event["markets"][0])
    game = schedule["dates"][0]["games"][0]
    game.update(game_changes)
    return market, event["event"], schedule


def test_mapping_policy_two_maps_the_unique_official_game(fixture_data):
    market, event, schedule = mapping_inputs(fixture_data)
    quarantined = resolve(market, event, schedule, reviewed_terms=True)
    assert quarantined["status"] == "quarantined"
    assert quarantined["reason"] == "unrecognized_full_game_rule"
    mapped = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert mapped["status"] == "mapped" and mapped["mapping_policy"] == 2
    assert mapped["game_id"] == 824948 and mapped["reason"] == "exact_original_date"
    assert original_date(rule_identity(market, V2)) == "2026-09-27"


def test_mapping_policy_two_follows_a_provider_linked_postponement(fixture_data):
    market, event, schedule = mapping_inputs(
        fixture_data,
        officialDate="2026-09-28",
        gameDate="2026-09-28T17:05:00Z",
        rescheduledFromDate="2026-09-27",
    )
    mapped = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert mapped["status"] == "mapped" and mapped["reason"] == "evidenced_schedule_change"
    assert mapped["scheduled_start"] == "2026-09-28T17:05:00Z"


@pytest.mark.parametrize(
    "game_changes,reason",
    [
        ({"doubleHeader": "S"}, "doubleheader_requires_game_number"),
        ({"officialDate": "2026-09-29"}, "unverified_schedule_identity"),
        ({"reverseHomeAwayStatus": True}, "home_away_reversed"),
    ],
)
def test_mapping_policy_two_quarantines_unresolvable_identity(fixture_data, game_changes, reason):
    market, event, schedule = mapping_inputs(fixture_data, **game_changes)
    result = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert result["status"] == "quarantined" and result["reason"] == reason


def test_mapping_policy_two_requires_ticker_corroboration(fixture_data):
    market, event, schedule = mapping_inputs(fixture_data)
    market["event_ticker"] = "KXMLBGAME-26SEP271505ATHHOU"
    event = dict(event, event_ticker=market["event_ticker"])
    result = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert result["status"] == "quarantined" and result["reason"] == "ticker_identity_mismatch"


def on_date(market, event, rule_day, ticker_code):
    """The contract rewritten to another original date: rule wording and event ticker."""
    ticker = f"KXMLBGAME-{ticker_code}HOUATH"
    market = dict(
        market,
        rules_primary=market["rules_primary"].replace("Sep 27, 2026", rule_day),
        event_ticker=ticker,
    )
    return market, dict(event, event_ticker=ticker)


def test_mapping_policy_two_never_maps_a_game_moved_onto_the_rule_date(fixture_data):
    """Review F18: the only HOU@ATH entry on Sep 27 was moved there from Sep 26. It backs
    the Sep 26 contract (the provider link names that date) and never the Sep 27 one, so
    one gamePk cannot satisfy two rule dates."""
    market, event, schedule = mapping_inputs(
        fixture_data,
        gamePk=900010,
        gameDate="2026-09-27T20:10:00Z",
        rescheduledFromDate="2026-09-26",
    )
    moved_onto = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert moved_onto["status"] == "quarantined" and moved_onto["game_id"] is None
    assert moved_onto["reason"] == "rescheduled_from_other_date"
    assert moved_onto["candidate_ids"] == [900010]
    earlier = on_date(market, event, "Sep 26, 2026", "26SEP261905")
    linked = resolve(*earlier, schedule, reviewed_terms=True, rules=V2)
    assert linked["status"] == "mapped" and linked["game_id"] == 900010
    assert linked["reason"] == "evidenced_schedule_change"


@pytest.mark.parametrize(
    "game_changes,reason",
    [
        # A timestamp-only link: 02:05Z on Sep 27 is 22:05 ET on Sep 26 (Kalshi's dates
        # are Eastern), so it names Sep 26, not the UTC date.
        ({"rescheduledFrom": "2026-09-27T02:05:00Z"}, "rescheduled_from_other_date"),
        (
            {"rescheduledFrom": "2026-09-26T23:05:00Z", "rescheduledFromDate": "2026-09-26"},
            "rescheduled_from_other_date",
        ),
        # Malformed or self-contradictory links prove nothing.
        ({"rescheduledFromDate": "Sep 26"}, "unverified_schedule_identity"),
        ({"rescheduledFrom": "2026-09-26"}, "unverified_schedule_identity"),
        (
            {"rescheduledFrom": "2026-09-25T23:05:00Z", "rescheduledFromDate": "2026-09-27"},
            "unverified_schedule_identity",
        ),
    ],
)
def test_mapping_policy_two_refuses_reschedule_links_naming_another_date(
    fixture_data, game_changes, reason
):
    market, event, schedule = mapping_inputs(fixture_data, **game_changes)
    result = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert result["status"] == "quarantined" and result["reason"] == reason


def test_mapping_policy_two_refuses_a_postponement_link_contradicted_by_its_timestamp(
    fixture_data,
):
    market, event, schedule = mapping_inputs(
        fixture_data,
        officialDate="2026-09-28",
        gameDate="2026-09-28T17:05:00Z",
        rescheduledFromDate="2026-09-27",
        rescheduledFrom="2026-09-26T19:05:00Z",
    )
    result = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert result["status"] == "quarantined"
    assert result["reason"] == "unverified_schedule_identity"


@pytest.mark.parametrize(
    "game_changes",
    [
        # Links that name the rule date itself (a same-day move, or a consistent
        # postponement record) keep mapping exactly as before.
        {"rescheduledFromDate": "2026-09-27"},
        {"rescheduledFrom": "2026-09-28T01:05:00Z"},  # 21:05 ET on Sep 27
        {
            "officialDate": "2026-09-28",
            "gameDate": "2026-09-28T17:05:00Z",
            "rescheduledFromDate": "2026-09-27",
            "rescheduledFrom": "2026-09-27T19:05:00Z",
        },
    ],
)
def test_mapping_policy_two_keeps_links_that_name_the_rule_date(fixture_data, game_changes):
    market, event, schedule = mapping_inputs(fixture_data, **game_changes)
    result = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert result["status"] == "mapped" and result["game_id"] == 824948


@pytest.mark.parametrize(
    "game_date", ["absent", None, "TBD", "2026-09-27T19:05:00", "2026-09-27", 1790535900]
)
def test_mapping_policy_two_requires_an_offset_aware_official_start(fixture_data, game_date):
    """Review F19: the provider gameDate must be an offset-aware ISO instant; a missing,
    offset-less or non-ISO start quarantines instead of mapping or raising."""
    market, event, schedule = mapping_inputs(fixture_data, gameDate=game_date)
    if game_date == "absent":
        del schedule["dates"][0]["games"][0]["gameDate"]
    result = resolve(market, event, schedule, reviewed_terms=True, rules=V2)
    assert result["status"] == "quarantined" and result["game_id"] is None
    assert result["reason"] == "start_time_unknown"
