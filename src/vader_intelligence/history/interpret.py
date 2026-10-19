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
from datetime import datetime, timezone

from ..mapping import rule_identity
from ..normalize import decode
from ..settlement.models import games as schedule_games
from ..settlement.models import instant

INTERPRETATION = "history-v1"
RULES = ("rule-v1", "rule-v2")
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


def identify(event_markets, games_by_id):
    """Identity of one event's pair of contracts with one official game, or a reason.

    Rule v1 (stated time) or rule v2 (date-only, ticker time) per contract; both contracts
    must agree; exactly one official game with these teams away/home on the date whose
    start equals the contract's start. Returns a dict with status identified|unresolved.
    """
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
            return {"status": "unresolved", "reason": str(exc)}
    if {(i["away_id"], i["home_id"]) for i in identities} != {
        (identities[0]["away_id"], identities[0]["home_id"])
    } or {i["team_id"] for i in identities} != {identities[0]["away_id"], identities[0]["home_id"]}:
        return {"status": "unresolved", "reason": "contract_pair_inconsistent"}
    first = identities[0]
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


def candles(db, ticker, run_id=None):
    """Parsed 1-minute candles of one ticker (both endpoint field variants), oldest first,
    each with the retrieval identity of the response it came from."""
    out = {}
    for fetch_id, retrieved_at, data, key in _bodies(db, run_id, "hist-candles"):
        if data.get("ticker") not in (None, ticker) or not key.startswith(f"candles:{ticker}:"):
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


def summary(db, run_id=None):
    """Counts for inspection: markets, events, identities by reason, candle coverage."""
    records = markets(db, run_id)
    games = schedule(db, run_id)
    by_reason = {}
    identified = 0
    for event_markets in events(records).values():
        result = identify(event_markets, games)
        if result["status"] == "identified":
            identified += 1
        else:
            by_reason[result["reason"]] = by_reason.get(result["reason"], 0) + 1
    return json.loads(
        json.dumps(
            {
                "interpretation": INTERPRETATION,
                "markets": len(records),
                "events": len(events(records)),
                "games": len(games),
                "identified_events": identified,
                "unresolved_by_reason": dict(sorted(by_reason.items())),
            }
        )
    )
