"""Conservative full-game mapping v1; no fuzzy aliases or inferred fair prices."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..provenance import json_text
from ..scope import ALIASES, RULE
from ..settlement.models import dollar, games, instant, mlb_result, validate_game

# Verified on live KXMLBGAME-26SEP251305CHCBOSG1, 2026-09-28.
MAPPING_RULE = re.compile(
    RULE.pattern.replace("game originally", "game(?: ([12]) of the double header)? originally")
)


def rule_identity(market):
    rule = market.get("rules_primary")
    match = MAPPING_RULE.fullmatch(rule) if isinstance(rule, str) else None
    if not match:
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


def resolve(market, event, schedule, *, reviewed_terms, prior=None):
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
        identity = rule_identity(market)
    except ValueError as exc:
        return reject(str(exc))
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
