"""Versioned interpretation of archived historical market data (interpretation history-v1).

Pure functions over raw response bytes archived by `history.acquire`. Nothing here
fetches, writes or reads a clock. Every record keeps the retrieval identity it came from.
The evidence kind of everything derived here is `historical_retrieval`: retrieved after
the fact, describing earlier times. It is never forward evidence and never witnessed.

Interpretation choices (each a modelling assumption, recorded in dataset manifests):
- A candle's values are taken as known at its `end_period_ts` (Kalshi computes candles
  after the fact; the archive cannot prove when they were visible).
- The official start used to define the cutoff is the start in the schedule as retrieved
  later. A game whose final start differs from the start its contract names does not
  identify (rule v1: stated time; rule v2: ticker time), so retrospective start changes
  are excluded rather than reinterpreted.
- Team-level features may use only games whose official date is before the game's own
  official date (computed by the dataset layer, not here).
"""

import json
import re
from datetime import datetime, timezone

from ..mapping import rule_identity
from ..normalize import decode
from ..scope import ALIASES, ticker_identity, ticker_teams_match
from ..settlement.models import games as schedule_games
from ..settlement.models import instant

INTERPRETATION = "history-v1"
RULES = ("rule-v1", "rule-v2")
# History-only identity rule for 2025-era contracts, which state no start time in either
# the rule ("...[originally] scheduled for Jul 4, 2025, then...", "vs" or "at") or
# the ticker (`KXMLBGAME-25JUL04CINPHI`). Identity is proven by the date and exact teams,
# corroborated by the ticker's date and team codes, and exactly one official game between
# them on that date (no doubleheader); the start is the later-retrieved official start and
# is NOT corroborated, so rows carry start_corroborated=false. Off by default; never used
# for forward eligibility.
DATE_ONLY_RULE = "history-date-only"
IDENTITY_RULES = ("default", DATE_ONLY_RULE)
RULE_DATE_ONLY = re.compile(
    r"If (.+) wins the (.+) (?:vs|at) (.+) professional baseball game (?:originally )?scheduled"
    r" for ([A-Z][a-z]{2} \d{1,2}, \d{4}), then the market resolves to Yes\."
)
TICKER_DATE_ONLY = re.compile(r"KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})([A-Z]+?)(G[12])?")
LISTING_STAGES = {"hist-markets": "historical", "hist-settled": "live"}


def _bodies(db, run_id, stage):
    """Successful archived bodies of one stage, oldest first, with retrieval identity."""
    query = (
        "SELECT f.id, f.retrieved_at, b.body, q.request_key FROM fetches f"
        " JOIN requests q ON q.id=f.request_id JOIN blobs b ON b.sha256=f.body_sha256"
        " WHERE q.stage=? AND f.state='ok'"
    )
    params = [stage]
    if run_id is not None:
        query += " AND q.run_id=?"
        params.append(run_id)
    for fetch_id, retrieved_at, body, key in db.execute(query + " ORDER BY f.seq", params):
        yield fetch_id, retrieved_at, decode(body), key


def cutoff(db, run_id=None):
    """Kalshi's historical cutoff as retrieved: market_settled_ts or None."""
    found = None
    for fetch_id, retrieved_at, data, _ in _bodies(db, run_id, "hist-cutoff"):
        found = {
            "market_settled_ts": data.get("market_settled_ts"),
            "fetch_id": fetch_id,
            "retrieved_at": retrieved_at,
        }
    return found


def markets(db, run_id=None):
    """Latest archived listing of every KXMLBGAME market: ticker → record."""
    out = {}
    for stage, source in LISTING_STAGES.items():
        for fetch_id, retrieved_at, data, _ in _bodies(db, run_id, stage):
            for market in data.get("markets", []):
                ticker = market.get("ticker")
                if not isinstance(ticker, str) or not ticker.startswith("KXMLBGAME-"):
                    continue
                out[ticker] = {
                    "ticker": ticker,
                    "event_ticker": market.get("event_ticker"),
                    "rules_primary": market.get("rules_primary"),
                    "yes_sub_title": market.get("yes_sub_title"),
                    "status": market.get("status"),
                    "result": market.get("result"),
                    "settlement_value": market.get(
                        "settlement_value_dollars", market.get("settlement_value")
                    ),
                    "settlement_ts": market.get("settlement_ts"),
                    "close_time": market.get("close_time"),
                    "market_type": market.get("market_type"),
                    "notional": market.get("notional_value_dollars"),
                    "source": source,
                    "fetch_id": fetch_id,
                    "retrieved_at": retrieved_at,
                    "raw": market,
                }
    return out


def schedule(db, run_id=None):
    """gamePk → latest archived official schedule entry (final state as retrieved)."""
    out = {}
    for fetch_id, retrieved_at, data, _ in _bodies(db, run_id, "hist-schedule"):
        for game in schedule_games(data):
            out[game["gamePk"]] = game | {"_fetch_id": fetch_id, "_retrieved_at": retrieved_at}
    return out


def event_date(event_ticker):
    """Official date named by an event ticker (timed or 2025 date-only form), or None."""
    timed = ticker_identity(event_ticker)
    if timed is not None:
        return timed["start"].date()
    match = TICKER_DATE_ONLY.fullmatch(event_ticker) if isinstance(event_ticker, str) else None
    if not match:
        return None
    try:
        return datetime.strptime(f"20{match[1]} {match[2].title()} {match[3]}", "%Y %b %d").date()
    except ValueError:
        return None


def _date_only_identity(market):
    match = RULE_DATE_ONLY.fullmatch(market.get("rules_primary") or "")
    if not match:
        raise ValueError("unrecognized_full_game_rule")
    winner, away, home, day = match.groups()
    if any(x not in ALIASES for x in (winner, away, home)):
        raise ValueError("unknown_team_alias")
    ids = [ALIASES[x] for x in (winner, away, home)]
    if (
        ids[1] == ids[2]
        or ids[0] not in ids[1:]
        or ALIASES.get(market.get("yes_sub_title")) != ids[0]
    ):
        raise ValueError("invalid_participants")
    original = datetime.strptime(day, "%b %d, %Y").date()
    ticker = TICKER_DATE_ONLY.fullmatch(market.get("event_ticker") or "")
    if (
        ticker is None
        or event_date(market["event_ticker"]) != original
        or not ticker_teams_match(ticker[4], ids[1], ids[2])
        or ticker[5] is not None
    ):
        raise ValueError("ticker_identity_mismatch")
    return {
        "team_id": ids[0],
        "away_id": ids[1],
        "home_id": ids[2],
        "original_date": original.isoformat(),
        "rule_version": DATE_ONLY_RULE,
    }


def identify(event_markets, games_by_id, identity_rules=("default",)):
    """Identity of one event's pair of contracts with one official game, or a reason.

    Rule v1 (stated time) or rule v2 (date-only, ticker time) per contract; both contracts
    must agree; exactly one official game with these teams away/home on the date whose
    start equals the contract's start. With `history-date-only` enabled, a 2025-era
    contract stating no time anywhere identifies by date and teams alone and its row is
    marked start_corroborated=false. Returns a dict with status identified|unresolved.
    """
    if not set(identity_rules) <= set(IDENTITY_RULES) or "default" not in identity_rules:
        raise ValueError("identity_rules must include 'default' and only known rules")
    if len(event_markets) != 2:
        return {"status": "unresolved", "reason": "not_two_contracts"}
    identities = []
    for market in event_markets:
        raw = market["raw"]
        if raw.get("market_type") not in (None, "binary"):
            return {"status": "unresolved", "reason": "unsupported_market_type"}
        try:
            identities.append(rule_identity(raw, RULES))
        except ValueError as exc:
            if DATE_ONLY_RULE not in identity_rules or str(exc) not in (
                "unrecognized_full_game_rule",
                "ticker_unparsed",
            ):
                return {"status": "unresolved", "reason": str(exc)}
            try:
                identities.append(_date_only_identity(raw))
            except ValueError as fallback:
                return {"status": "unresolved", "reason": str(fallback)}
    if {(i["away_id"], i["home_id"]) for i in identities} != {
        (identities[0]["away_id"], identities[0]["home_id"])
    } or {i["team_id"] for i in identities} != {identities[0]["away_id"], identities[0]["home_id"]}:
        return {"status": "unresolved", "reason": "contract_pair_inconsistent"}
    first = identities[0]
    if len({i.get("rule_version", 1) for i in identities}) != 1:
        return {"status": "unresolved", "reason": "contract_pair_inconsistent"}
    if first.get("rule_version") == DATE_ONLY_RULE:
        return _identify_date_only(event_markets, identities, games_by_id)
    if first.get("rule_version") == 2:
        start = instant(first["ticker_start"])
        day = first["original_date"]
        number = None
    else:
        start = instant(first["original_start"])
        day = start.astimezone(_eastern()).date().isoformat()
        number = first.get("game_number")
    candidates = []
    for game in games_by_id.values():
        teams = game.get("teams", {})
        pair = tuple(teams.get(s, {}).get("team", {}).get("id") for s in ("away", "home"))
        if pair != (first["away_id"], first["home_id"]) or game.get("officialDate") != day:
            continue
        if number is not None and game.get("gameNumber") != number:
            continue
        candidates.append(game)
    exact = [g for g in candidates if g.get("gameDate") and instant(g["gameDate"]) == start]
    if len(exact) != 1:
        return {
            "status": "unresolved",
            "reason": "ambiguous_game"
            if len(exact) > 1
            else ("start_differs" if candidates else "unmapped_game"),
        }
    game = exact[0]
    team_ids = sorted({first["away_id"], first["home_id"]})
    selected = next(m for m, i in zip(event_markets, identities) if i["team_id"] == team_ids[0])
    return {
        "status": "identified",
        "game_id": game["gamePk"],
        "start": start.astimezone(timezone.utc).isoformat(),
        "official_date": day,
        "away_id": first["away_id"],
        "home_id": first["home_id"],
        "rule_version": first.get("rule_version", 1),
        "selected_ticker": selected["ticker"],
        "selected_team_id": team_ids[0],
        "tickers": sorted(m["ticker"] for m in event_markets),
        "schedule_fetch_id": game["_fetch_id"],
        "game_status": (game.get("status") or {}).get("detailedState"),
        "start_corroborated": True,
    }


def _identify_date_only(event_markets, identities, games_by_id):
    first = identities[0]
    day = first["original_date"]
    same_day = []
    for game in games_by_id.values():
        teams = game.get("teams", {})
        pair = tuple(teams.get(s, {}).get("team", {}).get("id") for s in ("away", "home"))
        if game.get("officialDate") == day and set(pair) == {first["away_id"], first["home_id"]}:
            same_day.append((pair, game))
    if len({g["gamePk"] for _, g in same_day}) != 1:
        return {
            "status": "unresolved",
            "reason": "ambiguous_game" if same_day else "unmapped_game",
        }
    pair, game = same_day[0]
    if pair != (first["away_id"], first["home_id"]):
        return {"status": "unresolved", "reason": "home_away_reversed"}
    if game.get("doubleHeader", "N") != "N" or not game.get("gameDate"):
        return {"status": "unresolved", "reason": "doubleheader_or_start_unknown"}
    team_ids = sorted({first["away_id"], first["home_id"]})
    selected = next(m for m, i in zip(event_markets, identities) if i["team_id"] == team_ids[0])
    return {
        "status": "identified",
        "game_id": game["gamePk"],
        "start": instant(game["gameDate"]).astimezone(timezone.utc).isoformat(),
        "official_date": day,
        "away_id": first["away_id"],
        "home_id": first["home_id"],
        "rule_version": DATE_ONLY_RULE,
        "selected_ticker": selected["ticker"],
        "selected_team_id": team_ids[0],
        "tickers": sorted(m["ticker"] for m in event_markets),
        "schedule_fetch_id": game["_fetch_id"],
        "game_status": (game.get("status") or {}).get("detailedState"),
        "start_corroborated": False,
    }


def _eastern():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def events(market_records):
    """event_ticker → list of market records (sorted by ticker)."""
    out = {}
    for record in market_records.values():
        out.setdefault(record["event_ticker"], []).append(record)
    return {k: sorted(v, key=lambda m: m["ticker"]) for k, v in out.items() if k}


def _price(block, name):
    if not isinstance(block, dict):
        return None
    value = block.get(name + "_dollars", block.get(name))
    return value if isinstance(value, str) else None


def candles(db, ticker, run_id=None, *, stage="hist-candles"):
    """Parsed 1-minute candles of one ticker (both endpoint field variants), oldest first,
    each with the retrieval identity of the response it came from. Feature code reads only
    the default stage, whose windows end at the cutoff."""
    if stage not in ("hist-candles", "hist-candles-post"):
        raise ValueError("unknown candle stage")
    prefix = "candles" if stage == "hist-candles" else "post"
    out = {}
    for fetch_id, retrieved_at, data, key in _bodies(db, run_id, stage):
        if data.get("ticker") not in (None, ticker) or not key.startswith(f"{prefix}:{ticker}:"):
            continue
        for candle in data.get("candlesticks", []):
            end = candle.get("end_period_ts")
            if type(end) is not int:
                continue
            out[end] = {
                "end": datetime.fromtimestamp(end, timezone.utc),
                "yes_bid_close": _price(candle.get("yes_bid"), "close"),
                "yes_ask_close": _price(candle.get("yes_ask"), "close"),
                "price_close": _price(candle.get("price"), "close"),
                "price_mean": _price(candle.get("price"), "mean"),
                "volume": candle.get("volume_fp", candle.get("volume")),
                "open_interest": candle.get("open_interest_fp", candle.get("open_interest")),
                "fetch_id": fetch_id,
                "retrieved_at": retrieved_at,
            }
    return [out[k] for k in sorted(out)]


def execution_candles(db, ticker, run_id=None):
    """Candles on both sides of the cutoff, for execution simulation ONLY (never features):
    the feature window (ending at C) merged with the post-cutoff window, oldest first."""
    merged = {c["end"]: c for c in candles(db, ticker, run_id)}
    merged.update({c["end"]: c for c in candles(db, ticker, run_id, stage="hist-candles-post")})
    return [merged[k] for k in sorted(merged)]


def summary(db, run_id=None, identity_rules=("default",)):
    """Counts for inspection: markets, events, identities by reason."""
    records = markets(db, run_id)
    games = schedule(db, run_id)
    by_reason = {}
    identified = 0
    for event_markets in events(records).values():
        result = identify(event_markets, games, identity_rules)
        if result["status"] == "identified":
            identified += 1
        else:
            by_reason[result["reason"]] = by_reason.get(result["reason"], 0) + 1
    return json.loads(
        json.dumps(
            {
                "interpretation": INTERPRETATION,
                "identity_rules": list(identity_rules),
                "markets": len(records),
                "events": len(events(records)),
                "games": len(games),
                "identified_events": identified,
                "unresolved_by_reason": dict(sorted(by_reason.items())),
            }
        )
    )
