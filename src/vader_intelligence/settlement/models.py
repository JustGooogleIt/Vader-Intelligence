"""Provider interpretation v1. No result from one provider sets the other's result."""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from ..normalize import DECIMAL


def instant(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("timestamp lacks timezone")
    return result


def dollar(value):
    # Exact strings, including sub-cent payouts; never round or accept floats.
    if not isinstance(value, str) or len(value) > 40 or not DECIMAL.fullmatch(value):
        raise ValueError("payout must be a bounded decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid payout") from exc
    if not number.is_finite() or not 0 <= number <= 1 or number.as_tuple().exponent < -18:
        raise ValueError("invalid $1 payout")
    return number


def kalshi_result(market):
    fields = (
        "ticker",
        "event_ticker",
        "status",
        "result",
        "settlement_value_dollars",
        "settlement_ts",
        "updated_time",
        "created_time",
        "close_time",
        "expiration_time",
        "latest_expiration_time",
        "expected_expiration_time",
        "expiration_value",
        "notional_value_dollars",
        "market_type",
        "is_provisional",
    )
    data = {k: market[k] for k in fields if k in market}
    flags = []
    state, outcome = market.get("status"), market.get("result")
    known = {"initialized", "inactive", "active", "closed", "determined", "disputed", "amended"}
    data["lifecycle"] = (
        "finalized" if state == "finalized" else "pending" if state in known else "unknown"
    )
    if data["lifecycle"] == "unknown":
        flags.append("unknown_provider_status")
    if state == "amended":
        flags.append("provider_amended")
    for field in ("settlement_ts", "updated_time"):
        if market.get(field) is not None:
            try:
                instant(market[field])
            except ValueError:
                flags.append("invalid_" + field)
    if state == "finalized" and not market.get("settlement_ts"):
        flags.append("missing_settlement_ts")
    data.update(payout_kind="unknown", binary_outcome=None, yes_payout=None, no_payout=None)
    if market.get("settlement_value_dollars") is None:
        flags.append("missing_payout")
    else:
        try:
            value = dollar(market["settlement_value_dollars"])
            if dollar(market.get("notional_value_dollars")) != 1:
                raise ValueError("unsupported notional")
        except ValueError:
            flags.append("invalid_payout_or_notional")
        else:
            data["yes_payout"] = market["settlement_value_dollars"]
            data["no_payout"] = format(Decimal(1) - value, "f")
            data["no_payout_derivation"] = "1 - settlement_value_dollars"
            binary = (outcome == "yes" and value == 1) or (outcome == "no" and value == 0)
            data["payout_kind"] = "binary" if binary else "exceptional"
            if outcome not in ("yes", "no", "scalar"):
                flags.append("unknown_provider_result")
            elif outcome in ("yes", "no") and not binary:
                flags.append("result_payout_conflict")
            if (
                binary
                and state == "finalized"
                and not flags
                and market.get("market_type") == "binary"
            ):
                data["binary_outcome"] = outcome
    data["flags"] = flags
    return data


def games(schedule):
    dates = schedule.get("dates")
    if not isinstance(dates, list):
        raise ValueError("MLB response lacks dates array")
    result = []
    for day in dates:
        if not isinstance(day, dict) or not isinstance(day.get("games"), list):
            raise ValueError("invalid MLB schedule day")
        result.extend(day["games"])
        if len(result) > 1000:
            raise ValueError("MLB schedule exceeds 1000 games")
    if any(not isinstance(g, dict) or type(g.get("gamePk")) is not int for g in result):
        raise ValueError("MLB game lacks integer gamePk")
    ids = [g["gamePk"] for g in result]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate MLB game identity in response")
    return result


def mlb_result(game):
    validate_game(game)
    fields = (
        "gamePk",
        "gameDate",
        "officialDate",
        "gameNumber",
        "doubleHeader",
        "status",
        "gameType",
        "isTie",
        "rescheduledFrom",
        "rescheduledFromDate",
        "rescheduleDate",
        "rescheduleGameDate",
        "resumedFrom",
        "resumeDate",
        "resumeGameDate",
        "reverseHomeAwayStatus",
        "description",
        "updatedAt",
        "lastUpdated",
    )
    data = {k: game[k] for k in fields if k in game}
    teams = game.get("teams", {})
    data["teams"] = {
        side: {
            k: teams.get(side, {})[k]
            for k in ("team", "score", "isWinner")
            if k in teams.get(side, {})
        }
        for side in ("away", "home")
    }
    status = game.get("status", {})
    detail = status.get("detailedState", "").lower()
    state = "pending"
    for word in ("postponed", "cancelled", "canceled", "suspended"):
        if word in detail:
            state = "cancelled" if word == "canceled" else word
            break
    else:
        if status.get("abstractGameState") == "Final":
            state = "final"
        elif status.get("abstractGameState") not in ("Preview", "Live"):
            state = "unknown"
    data.update(lifecycle=state, winner_team_id=None, flags=[])
    winners = [
        v.get("team", {}).get("id") for v in data["teams"].values() if v.get("isWinner") is True
    ]
    if state == "final" and game.get("isTie") is not True:
        if len(winners) == 1 and type(winners[0]) is int:
            data["winner_team_id"] = winners[0]
        else:
            data["flags"].append("missing_or_ambiguous_winner")
    # MLB schedule has no guaranteed finalization timestamp. Never invent one.
    return data


def validate_game(game):
    if not isinstance(game.get("status", {}), dict) or not isinstance(game.get("teams", {}), dict):
        raise ValueError("invalid MLB status or teams")
    if not isinstance(game.get("status", {}).get("detailedState", ""), str):
        raise ValueError("invalid MLB detailed state")
    for side in ("away", "home"):
        team = game.get("teams", {}).get(side, {})
        if not isinstance(team, dict) or not isinstance(team.get("team", {}), dict):
            raise ValueError("invalid MLB team")
