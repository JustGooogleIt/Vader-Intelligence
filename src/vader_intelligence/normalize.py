import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .provenance import json_text

PARSER_VERSION = 1
DECIMAL = re.compile(r"\d+(?:\.\d+)?\Z", re.ASCII)


def decode(body):
    def reject_constant(value):
        raise ValueError("nonfinite JSON constant")

    result = json.loads(body, parse_constant=reject_constant)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def decimal_value(value, *, price=False):
    if not isinstance(value, str) or len(value) > 40 or not DECIMAL.fullmatch(value):
        raise ValueError("expected a bounded nonnegative decimal string")
    if "." in value and len(value.split(".", 1)[1]) > (4 if price else 2):
        raise ValueError("decimal precision exceeds the documented source representation")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if price and number > 1:
        raise ValueError("binary contract price exceeds one dollar")
    return number


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9_-]{1,160}", value):
        raise ValueError("invalid provider identifier")
    return value


def entity(db, fetch_id, kind, ticker, data, parent=None):
    db.execute(
        "INSERT OR IGNORE INTO entities(fetch_id,kind,ticker,parent_ticker,parser_version,data_json) "
        "VALUES (?,?,?,?,?,?)",
        (fetch_id, kind, ticker, parent, PARSER_VERSION, json_text(data)),
    )


def observation(db, fetch_id, ticker, kind, data):
    db.execute(
        "INSERT OR IGNORE INTO observations(fetch_id,ticker,kind,parser_version,data_json) "
        "VALUES (?,?,?,?,?)",
        (fetch_id, ticker, kind, PARSER_VERSION, json_text(data)),
    )


def market(db, fetch_id, data):
    ticker = identifier(data["ticker"])
    parent = identifier(data["event_ticker"])
    values = {}
    for key in (
        "yes_bid_dollars",
        "yes_ask_dollars",
        "no_bid_dollars",
        "no_ask_dollars",
        "last_price_dollars",
        "volume_fp",
        "volume_24h_fp",
        "open_interest_fp",
        "yes_bid_size_fp",
        "yes_ask_size_fp",
    ):
        if key in data:
            if data[key] is not None:
                decimal_value(data[key], price=key.endswith("_dollars"))
            values[key] = data[key]
    values["status"] = data.get("status")
    entity(db, fetch_id, "market", ticker, data, parent)
    observation(db, fetch_id, ticker, "market", values)


def book_data(data):
    if "orderbook_fp" not in data or not isinstance(data["orderbook_fp"], dict):
        raise ValueError("orderbook_fp object is required")
    book = data["orderbook_fp"]
    result = {"bids": book, "flags": [], "side_state": {}}
    best = {}
    for side in ("yes", "no"):
        key = side + "_dollars"
        levels = book.get(key)
        result["side_state"][side] = (
            "missing"
            if key not in book
            else "null"
            if levels is None
            else "empty"
            if levels == []
            else "present"
        )
        if levels is None:
            best[side] = None
            continue
        if not isinstance(levels, list) or len(levels) > 10000:
            raise ValueError("invalid or oversized order book levels")
        parsed = []
        seen = set()
        for level in levels:
            if not isinstance(level, list) or len(level) != 2:
                raise ValueError("book level must contain price and quantity")
            price = decimal_value(level[0], price=True)
            quantity = decimal_value(level[1])
            if price in seen:
                raise ValueError("duplicate book price level")
            seen.add(price)
            if quantity > 0:
                parsed.append((price, level[1]))
        if parsed != sorted(parsed):
            result["flags"].append(side + "_unsorted")
        best[side] = max(parsed, default=None)
    for side, other in (("yes", "no"), ("no", "yes")):
        bid = best[side][0] if best[side] else None
        ask = Decimal(1) - best[other][0] if best[other] else None
        spread = ask - bid if bid is not None and ask is not None else None
        result[side + "_bid_dollars"] = str(bid) if bid is not None else None
        result[side + "_ask_dollars"] = str(ask) if ask is not None else None
        result[side + "_spread_dollars"] = str(spread) if spread is not None else None
        result[side + "_bid_size_fp"] = best[side][1] if best[side] else None
        result[side + "_ask_size_fp"] = best[other][1] if best[other] else None
    if result["yes_spread_dollars"] is not None and Decimal(result["yes_spread_dollars"]) < 0:
        result["flags"].append("crossed_book")
    return result


def normalizer(stage, key=""):
    def apply(db, fetch_id, body):
        if stage == "document":
            if not body.startswith(b"%PDF-"):
                raise ValueError("contract document is not a PDF")
            entity(db, fetch_id, "document", key, {"url": key, "media_type": "application/pdf"})
            return {}
        data = decode(body)
        if stage == "markets":
            markets = data.get("markets")
            if not isinstance(markets, list) or len(markets) > 1000:
                raise ValueError("invalid market page")
            tickers = set()
            for item in markets:
                if item["ticker"] in tickers:
                    raise ValueError("duplicate market within one page")
                tickers.add(item["ticker"])
                market(db, fetch_id, item)
            cursor = data.get("cursor")
            if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
                raise ValueError("invalid pagination cursor")
            return {"cursor": cursor}
        if stage == "series":
            item = data["series"]
            entity(db, fetch_id, "series", identifier(item["ticker"]), item)
        elif stage == "event":
            item = data["event"]
            entity(
                db,
                fetch_id,
                "event",
                identifier(item["event_ticker"]),
                item,
                identifier(item["series_ticker"]),
            )
            if not isinstance(data.get("markets"), list) or len(data["markets"]) > 100:
                raise ValueError("event market list required and bounded")
            for item in data["markets"]:
                market(db, fetch_id, item)
        elif stage == "schedule":
            dates = data.get("dates")
            if not isinstance(dates, list) or len(dates) > 32:
                raise ValueError("invalid schedule date window")
            for date in dates:
                if not isinstance(date, dict):
                    raise ValueError("schedule date must be an object")
                games = date.get("games")
                if not isinstance(games, list) or len(games) > 100:
                    raise ValueError("invalid schedule games")
                for game in games:
                    entity(db, fetch_id, "mlb_game", str(game["gamePk"]), game)
        elif stage == "book":
            proof = db.execute(
                "SELECT e.*, f.started_at, f.retrieved_at, s.retrieved_at AS schedule_at "
                "FROM fetches f JOIN requests r ON r.id=f.request_id "
                "JOIN eligibility e ON e.run_id=r.run_id AND e.ticker=? "
                "JOIN fetches s ON s.id=e.schedule_fetch_id "
                "WHERE f.id=? AND e.checked_at<=f.started_at ORDER BY e.id DESC LIMIT 1",
                (key, fetch_id),
            ).fetchone()
            if proof is None or not proof["eligible"] or not proof["cutoff"]:
                raise ValueError("book lacks verified pregame eligibility")
            if datetime.fromisoformat(proof["retrieved_at"]) >= datetime.fromisoformat(
                proof["cutoff"]
            ):
                raise ValueError("book arrived at or after pregame cutoff")
            age = (
                datetime.fromisoformat(proof["started_at"])
                - datetime.fromisoformat(proof["schedule_at"])
            ).total_seconds()
            if not 0 <= age <= 120:
                raise ValueError("book request has stale schedule evidence")
            parsed = book_data(data)
            parsed["eligibility_id"] = proof["id"]
            parsed["game_id"] = proof["game_id"]
            parsed["cutoff"] = proof["cutoff"]
            observation(db, fetch_id, identifier(key), "book", parsed)
        elif stage == "status":
            entity(db, fetch_id, "exchange", "kalshi", data)
        else:
            raise ValueError("unknown normalization stage")
        return {}

    return apply
