"""Conservative full-game mapping v1; no fuzzy aliases or inferred fair prices."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import DEFAULT_RULES
from ..provenance import json_text
from ..scope import ALIASES, RULE, RULE_V2, ticker_identity, ticker_teams_match
from ..settlement.models import dollar, games, instant, mlb_result, validate_game

# Verified on live KXMLBGAME-26SEP251305CHCBOSG1, 2026-09-28.
MAPPING_RULE = re.compile(
    RULE.pattern.replace("game originally", "game(?: ([12]) of the double header)? originally")
)
# Mapping policy 2 (decision D-002): the date-only postseason wording.
MAPPING_RULE_V2 = re.compile(
    RULE_V2.pattern.replace("game originally", "game(?: ([12]) of the double header)? originally")
)


def rule_identity(market, rules=DEFAULT_RULES):
    rule = market.get("rules_primary")
    match = MAPPING_RULE.fullmatch(rule) if isinstance(rule, str) else None
    if not match:
        dated = MAPPING_RULE_V2.fullmatch(rule) if isinstance(rule, str) else None
        if dated and "rule-v2" in rules:
            return _rule_identity_v2(market, dated)
        raise ValueError("unrecognized_full_game_rule")
    winner, away, home, rule_number, day, clock, zone = match.groups()
    if any(x not in ALIASES for x in (winner, away, home)):
        raise ValueError("unknown_team_alias")
    ids = [ALIASES[x] for x in (winner, away, home)]
    if (
        ids[1] == ids[2]
        or ids[0] not in ids[1:]
        or ALIASES.get(market.get("yes_sub_title")) != ids[0]
    ):
        raise ValueError("invalid_participants")
    original = datetime.strptime(day + " " + clock, "%b %d, %Y %I:%M %p").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    if original.tzname() != zone:
        raise ValueError("rule_timezone_mismatch")
    suffix = re.search(r"G([12])$", market.get("event_ticker", ""))
    if rule_number and suffix and rule_number != suffix[1]:
        raise ValueError("conflicting_game_number")
    return {
        "team_id": ids[0],
        "away_id": ids[1],
        "home_id": ids[2],
        "original_start": original.isoformat(),
        "game_number": int(rule_number) if rule_number else int(suffix[1]) if suffix else None,
    }


def _rule_identity_v2(market, match):
    """Identity of a date-only rule: participants, YES team and original date, with the
    event ticker as required corroboration (same date and teams, no game number)."""
    winner, away, home, rule_number, day = match.groups()
    if any(x not in ALIASES for x in (winner, away, home)):
        raise ValueError("unknown_team_alias")
    ids = [ALIASES[x] for x in (winner, away, home)]
    if (
        ids[1] == ids[2]
        or ids[0] not in ids[1:]
        or ALIASES.get(market.get("yes_sub_title")) != ids[0]
    ):
        raise ValueError("invalid_participants")
    if rule_number:
        raise ValueError("doubleheader_requires_game_number")
    original = datetime.strptime(day, "%b %d, %Y").date()
    ticker = ticker_identity(market.get("event_ticker"))
    if ticker is None:
        raise ValueError("ticker_unparsed")
    if (
        ticker["start"].date() != original
        or not ticker_teams_match(ticker["teams"], ids[1], ids[2])
        or ticker["game_number"] is not None
    ):
        raise ValueError("ticker_identity_mismatch")
    return {
        "team_id": ids[0],
        "away_id": ids[1],
        "home_id": ids[2],
        "original_date": original.isoformat(),
        "ticker_start": ticker["start"].isoformat(),
        "rule_version": 2,
    }


def original_date(identity):
    """The rule's original official date for either mapping policy."""
    if identity.get("original_date"):
        return identity["original_date"]
    return instant(identity["original_start"]).date().isoformat()


def resolve(market, event, schedule, *, reviewed_terms, prior=None, rules=DEFAULT_RULES):
    result = {
        "status": "quarantined",
        "reason": None,
        "game_id": None,
        "candidate_ids": [],
        "mapping_policy": 1,
    }

    def reject(reason):
        result["reason"] = reason
        return result

    if not reviewed_terms:
        return reject("unreviewed_contract_terms")
    metadata = event.get("product_metadata", {})
    if not isinstance(metadata, dict):
        return reject("unsupported_event_scope")
    if (
        event.get("series_ticker") != "KXMLBGAME"
        or metadata.get("competition") != "Pro Baseball"
        or metadata.get("competition_scope") != "Game"
        or market.get("event_ticker") != event.get("event_ticker")
        or market.get("market_type") != "binary"
    ):
        return reject("unsupported_event_scope")
    try:
        if dollar(market.get("notional_value_dollars")) != 1:
            return reject("unsupported_notional")
        identity = rule_identity(market, rules)
    except ValueError as exc:
        return reject(str(exc))
    if identity.get("rule_version") == 2:
        return _resolve_v2(schedule, identity, result)
    result.update(identity)
    original = instant(identity["original_start"])
    candidates, reversed_ids, related_ids = [], [], []
    variants = {}
    for game in games(schedule):
        validate_game(game)
        variants.setdefault(game["gamePk"], set()).add(json_text(mlb_result(game)))
        teams = game.get("teams", {})
        pair = tuple(teams.get(s, {}).get("team", {}).get("id") for s in ("away", "home"))
        if (
            pair == (identity["home_id"], identity["away_id"])
            and game.get("officialDate") == original.date().isoformat()
        ):
            reversed_ids.append(game["gamePk"])
        if pair != (identity["away_id"], identity["home_id"]):
            continue
        related_ids.append(game["gamePk"])
        if (
            identity["game_number"] is not None
            and game.get("gameNumber") != identity["game_number"]
        ):
            continue
        trusted = (
            prior
            and prior.get("status") == "mapped"
            and all(prior.get(k) == identity[k] for k in identity)
            and prior.get("game_id") == game["gamePk"]
        )
        try:
            exact = (
                instant(game.get("gameDate")) == original
                and game.get("officialDate") == original.date().isoformat()
            )
            # Only a timestamp-level provider link proves identity for rescheduling.
            linked = (
                game.get("rescheduledFrom") is not None
                and instant(game["rescheduledFrom"]) == original
            )
        except ValueError:
            exact, linked = False, False
        if exact or linked or trusted:
            candidates.append(game)
    candidates = list({g["gamePk"]: g for g in candidates}.values())
    result["candidate_ids"] = sorted(
        {g["gamePk"] for g in candidates} or set(related_ids) or set(reversed_ids)
    )
    if any(len(variants[g["gamePk"]]) > 1 for g in candidates):
        return reject("conflicting_schedule_entries")
    if len(candidates) != 1:
        return reject(
            "ambiguous_game"
            if candidates
            else "home_away_reversed"
            if reversed_ids
            else "unverified_schedule_identity"
        )
    game = candidates[0]
    if game.get("reverseHomeAwayStatus") is True:
        return reject("home_away_reversed")
    if game.get("gameType") not in {"R", "F", "D", "L", "W"}:
        return reject("unsupported_game_type")
    if game.get("status", {}).get("startTimeTBD") is not False:
        return reject("start_time_unknown")
    changed = game.get("gameDate") is None or instant(game["gameDate"]) != original
    result.update(
        status="mapped",
        reason="evidenced_schedule_change" if changed else "exact_original_start",
        game_id=game["gamePk"],
        scheduled_start=game.get("gameDate"),
        official_date=game.get("officialDate"),
        official_game_number=game.get("gameNumber"),
        mlb_status=game.get("status"),
        prior_mapping_used=bool(changed and prior and prior.get("game_id") == game["gamePk"]),
    )
    return result


def _resolve_v2(schedule, identity, result):
    """Mapping policy 2: exactly one official game between the two teams on the original
    official date (or provider-linked to it by rescheduledFromDate), away/home as stated.
    No prior-mapping reuse; doubleheaders and conflicting entries are quarantined."""
    result.update(identity, mapping_policy=2)

    def reject(reason):
        result["reason"] = reason
        return result

    day = identity["original_date"]
    candidates, reversed_ids, variants = [], [], {}
    for game in games(schedule):
        validate_game(game)
        variants.setdefault(game["gamePk"], set()).add(json_text(mlb_result(game)))
        teams = game.get("teams", {})
        pair = tuple(teams.get(s, {}).get("team", {}).get("id") for s in ("away", "home"))
        on_day = game.get("officialDate") == day or game.get("rescheduledFromDate") == day
        if pair == (identity["home_id"], identity["away_id"]) and on_day:
            reversed_ids.append(game["gamePk"])
        elif pair == (identity["away_id"], identity["home_id"]) and on_day:
            candidates.append(game)
    candidates = list({g["gamePk"]: g for g in candidates}.values())
    result["candidate_ids"] = sorted({g["gamePk"] for g in candidates} | set(reversed_ids))
    if any(len(variants[g["gamePk"]]) > 1 for g in candidates):
        return reject("conflicting_schedule_entries")
    if len(result["candidate_ids"]) != 1 or not candidates:
        return reject(
            "ambiguous_game"
            if len(result["candidate_ids"]) > 1
            else "home_away_reversed"
            if reversed_ids
            else "unverified_schedule_identity"
        )
    game = candidates[0]
    if game.get("reverseHomeAwayStatus") is True:
        return reject("home_away_reversed")
    if game.get("gameType") not in {"R", "F", "D", "L", "W"}:
        return reject("unsupported_game_type")
    if game.get("doubleHeader", "N") != "N":
        return reject("doubleheader_requires_game_number")
    if game.get("status", {}).get("startTimeTBD") is not False:
        return reject("start_time_unknown")
    exact = (
        game.get("officialDate") == day
        and game.get("gameDate") is not None
        and instant(game["gameDate"]) == instant(identity["ticker_start"])
    )
    result.update(
        status="mapped",
        reason="exact_original_date" if exact else "evidenced_schedule_change",
        game_id=game["gamePk"],
        scheduled_start=game.get("gameDate"),
        official_date=game.get("officialDate"),
        official_game_number=game.get("gameNumber"),
        mlb_status=game.get("status"),
        prior_mapping_used=False,
    )
    return result
