"""All functions run inside the caller's response/archive transaction."""

import hashlib
import json

from ..mapping import resolve
from ..normalize import decode
from ..provenance import json_text
from .models import games, kalshi_result, mlb_result

PARSER_VERSION = 1
TERMS_URL = "https://assets.kalshi.com/contract_terms/BASEBALLGAMEWIN.pdf"
REVIEWED_TERMS = {"46b02443153f4692acb3bac3d3aedabe93e837b08c80323013c8dce117ebb6e7"}


def latest(db, kind, key, *, before_seq=None):
    row = db.execute(
        "SELECT v.* FROM settlement_versions v JOIN fetches f ON f.id=v.first_fetch_id "
        "WHERE v.kind=? AND v.entity_key=? AND (? IS NULL OR f.seq<?) "
        "ORDER BY v.revision DESC LIMIT 1",
        (kind, str(key), before_seq, before_seq),
    ).fetchone()
    return row


def record(db, kind, key, fetch_id, data):
    key = str(key)
    seen = db.execute(
        "SELECT version_id FROM settlement_observations WHERE kind=? AND entity_key=? AND fetch_id=? AND parser_version=?",
        (kind, key, fetch_id, PARSER_VERSION),
    ).fetchone()
    if seen:
        return seen[0]
    encoded = json_text(data)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    old = latest(db, kind, key)
    if old and old["digest"] == digest:
        version = old["id"]
    else:
        # Replaying older unseen evidence after newer materialization would change
        # revision history. Require ordered rebuild instead of silently corrupting it.
        seq = db.execute("SELECT seq FROM fetches WHERE id=?", (fetch_id,)).fetchone()[0]
        last_seq = db.execute(
            "SELECT MAX(f.seq) FROM settlement_observations o JOIN fetches f ON f.id=o.fetch_id WHERE o.kind=? AND o.entity_key=?",
            (kind, key),
        ).fetchone()[0]
        if last_seq is not None and seq < last_seq:
            raise ValueError(
                "out-of-order materialization requires ordered replay in an isolated database"
            )
        change = "initial"
        if old:
            previous = json.loads(old["data_json"])
            if kind == "kalshi":
                changed_result = any(
                    previous.get(k) != data.get(k) for k in ("result", "settlement_value_dollars")
                )
                change = (
                    "corrected"
                    if changed_result and previous.get("yes_payout") is not None
                    else "updated"
                )
            elif kind == "mlb":
                change = (
                    "corrected"
                    if previous.get("lifecycle") == "final"
                    and any(
                        previous.get(k) != data.get(k)
                        for k in ("teams", "winner_team_id", "isTie", "lifecycle")
                    )
                    else "updated"
                )
            else:
                change = "updated"
        if kind == "kalshi" and data.get("status") == "amended":
            change = "provider_amended"
        version = db.execute(
            "INSERT INTO settlement_versions(kind,entity_key,revision,previous_id,digest,data_json,change_kind,first_fetch_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                kind,
                key,
                old["revision"] + 1 if old else 1,
                old["id"] if old else None,
                digest,
                encoded,
                change,
                fetch_id,
            ),
        ).lastrowid
    db.execute(
        "INSERT INTO settlement_observations VALUES (?,?,?,?,?)",
        (kind, key, fetch_id, version, PARSER_VERSION),
    )
    return version


def body(db, fetch_id):
    row = db.execute(
        "SELECT b.body,b.sha256 FROM fetches f JOIN blobs b ON b.sha256=f.body_sha256 WHERE f.id=?",
        (fetch_id,),
    ).fetchone()
    if row is None or hashlib.sha256(row["body"]).hexdigest() != row["sha256"]:
        raise ValueError("missing or corrupt supporting archive")
    return row["body"]


def map_target(db, target_id):
    target = db.execute("SELECT * FROM settlement_targets WHERE id=?", (target_id,)).fetchone()
    if target["mapping_version_id"] is not None:
        return
    market = decode(body(db, target["market_fetch_id"]))["market"]
    event = decode(body(db, target["event_fetch_id"]))["event"] if target["event_fetch_id"] else {}
    schedule = (
        decode(body(db, target["schedule_fetch_id"]))
        if target["schedule_fetch_id"]
        else {"dates": []}
    )
    anchor = target["schedule_fetch_id"] or target["event_fetch_id"] or target["market_fetch_id"]
    seq = db.execute("SELECT seq FROM fetches WHERE id=?", (anchor,)).fetchone()[0]
    previous = latest(db, "mapping", target["ticker"], before_seq=seq)
    prior = json.loads(previous["data_json"]) if previous else None
    terms_digest = hashlib.sha256(body(db, target["terms_fetch_id"])).hexdigest()
    data = resolve(
        market, event, schedule, reviewed_terms=terms_digest in REVIEWED_TERMS, prior=prior
    )
    # The target retains the complete evidence tuple even when the semantic mapping
    # is unchanged. Prior mapping ID is included only when required to prove identity.
    if data.get("prior_mapping_used"):
        data["prior_mapping_version_id"] = prior.get("prior_mapping_version_id", previous["id"])
    version = record(db, "mapping", target["ticker"], anchor, data)
    db.execute(
        "UPDATE settlement_targets SET mapping_version_id=?,state=? WHERE id=?",
        (version, data["status"], target_id),
    )


def normalizer(stage, key):
    def apply(db, fetch_id, raw):
        data = decode(raw)
        if stage == "settlement-discovery":
            markets = data.get("markets")
            if not isinstance(markets, list) or len(markets) > 1000:
                raise ValueError("invalid bounded market page")
            if any(not isinstance(m, dict) for m in markets):
                raise ValueError("invalid discovery market")
            tickers = [m.get("ticker") for m in markets]
            if any(not isinstance(t, str) or not t.startswith("KXMLBGAME-") for t in tickers):
                raise ValueError("unexpected discovery family")
            if not isinstance(data.get("cursor", ""), str) or len(data.get("cursor", "")) > 4096:
                raise ValueError("invalid cursor")
            return {"tickers": tickers, "cursor": data.get("cursor", "")}
        target = db.execute("SELECT * FROM settlement_targets WHERE id=?", (key,)).fetchone()
        if target is None:
            raise ValueError("settlement target missing")
        if stage == "settlement-market":
            market = data.get("market")
            if not isinstance(market, dict) or market.get("ticker") != target["ticker"]:
                raise ValueError("market identity mismatch")
            record(db, "kalshi", target["ticker"], fetch_id, kalshi_result(market))
            column = "market_fetch_id"
        elif stage == "settlement-event":
            event = data.get("event")
            market = decode(body(db, target["market_fetch_id"]))["market"]
            if not isinstance(event, dict) or event.get("event_ticker") != market.get(
                "event_ticker"
            ):
                raise ValueError("event identity mismatch")
            column = "event_fetch_id"
        elif stage == "settlement-schedule":
            for game in games(data):
                record(db, "mlb", game["gamePk"], fetch_id, mlb_result(game))
            column = "schedule_fetch_id"
        else:
            raise ValueError("unknown settlement stage")
        # Successful request identity is immutable, including during replay.
        if target[column] not in (None, fetch_id):
            raise ValueError("target already references another successful retrieval")
        db.execute(f"UPDATE settlement_targets SET {column}=? WHERE id=?", (fetch_id, key))
        if stage == "settlement-schedule":
            map_target(db, key)
        return {"target_id": key, "fetch_id": fetch_id}

    return apply
