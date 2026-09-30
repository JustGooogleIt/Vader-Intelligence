import json
import re
import uuid
from dataclasses import asdict
from datetime import timedelta

from ..mapping import rule_identity
from ..normalize import decode
from ..provenance import RunProvenanceV1, source_hash
from ..transport import KALSHI, MLB_SCHEDULE, CollectionError, DeadlineExceeded, HTTPFailure
from .journal import TERMS_URL, latest, map_target, normalizer
from .models import instant
from .schema import require_schema


def ticker(value):
    if not isinstance(value, str) or not re.fullmatch(r"KXMLBGAME-[A-Z0-9_-]{1,100}", value):
        raise ValueError("invalid MLB winner ticker")
    return value


class SettlementRefresh:
    def __init__(self, store, reader, config):
        self.store, self.reader, self.config = store, reader, config

    def run(self, *, tickers=(), limit=20, max_pages=2, budget=120, status="settled", resume=None):
        require_schema(self.store)
        if not 1 <= limit <= 50 or not 1 <= max_pages <= 5 or not 1 <= budget <= 300:
            raise ValueError("require limit 1..50, max-pages 1..5, budget 1..300 seconds")
        if limit > self.config.max_markets or max_pages > self.config.max_pages:
            raise ValueError("settlement limits exceed configured market/page limits")
        if status not in ("settled", "closed", "all"):
            raise ValueError("unsupported discovery status")
        targets = sorted({ticker(t) for t in tickers})
        if len(targets) > limit:
            raise ValueError("explicit targets exceed limit")
        options = {
            "tickers": targets,
            "limit": limit,
            "max_pages": max_pages,
            "budget": budget,
            "status": status,
        }
        configuration = {"collector": asdict(self.config), "settlement": options}
        run_id = resume or str(uuid.uuid4())
        if resume:
            row = self.store.db.execute(
                "SELECT * FROM runs WHERE id=? AND kind='settlement-refresh'", (resume,)
            ).fetchone()
            if row is None or row["status"] in ("complete", "inconclusive"):
                raise ValueError("resume requires an unfinished settlement run")
            provenance = json.loads(row["provenance_json"])
            if (
                provenance["code_hash"] != source_hash()
                or provenance["configuration"] != configuration
            ):
                raise ValueError("resume requires identical code, configuration and options")
            self.store.recover(resume)
        else:
            self.store.start_run(
                RunProvenanceV1(
                    run_id,
                    "settlement-refresh",
                    source_hash(),
                    configuration,
                    tool_configuration={
                        "mode": "public_get",
                        "settlement_parser": 1,
                        "database_schema": 2,
                    },
                )
            )
        end = self.reader.monotonic() + budget
        errors, selection_truncated = [], False

        def check_deadline():
            if self.reader.monotonic() >= end:
                raise DeadlineExceeded("settlement refresh budget exhausted")

        def get(stage, key, url, params=None):
            check_deadline()
            apply = normalizer(stage, key) if stage.startswith("settlement-") else None
            return self.reader.get(
                run_id,
                stage,
                key,
                url,
                params,
                apply=apply,
                budget=end - self.reader.monotonic(),
                before_attempt=check_deadline,
            )

        try:
            series = decode(get("series", "KXMLBGAME", KALSHI + "/series/KXMLBGAME").body)["series"]
            if series.get("contract_terms_url") != TERMS_URL:
                raise ValueError("unreviewed series contract terms URL")
            terms = get("document", TERMS_URL, TERMS_URL)
            if not targets:
                cursor, seen = "", set()
                for page in range(max_pages):
                    params = {
                        "series_ticker": "KXMLBGAME",
                        "limit": min(limit, self.config.page_size),
                    }
                    if status != "all":
                        params["status"] = status
                    if cursor:
                        params["cursor"] = cursor
                    response = get("settlement-discovery", str(page), KALSHI + "/markets", params)
                    data = decode(response.body)
                    for market in data["markets"]:
                        value = ticker(market["ticker"])
                        if value not in targets:
                            targets.append(value)
                    cursor = data.get("cursor", "")
                    if len(targets) >= limit:
                        selection_truncated = bool(cursor) or len(targets) > limit
                        targets = targets[:limit]
                        break
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("settlement discovery cursor loop")
                    seen.add(cursor)
                else:
                    selection_truncated = bool(cursor)
            for value in targets:
                check_deadline()
                with self.store.transaction():
                    self.store.db.execute(
                        "INSERT INTO settlement_targets(id,run_id,ticker,terms_fetch_id) VALUES (?,?,?,?) ON CONFLICT(run_id,ticker) DO NOTHING",
                        (str(uuid.uuid4()), run_id, value, terms.fetch_id),
                    )
                    target = self.store.db.execute(
                        "SELECT * FROM settlement_targets WHERE run_id=? AND ticker=?",
                        (run_id, value),
                    ).fetchone()
                if target["state"] != "pending":
                    continue
                try:
                    market = decode(
                        get("settlement-market", target["id"], KALSHI + "/markets/" + value).body
                    )["market"]
                    event_id = ticker(market.get("event_ticker"))
                    get("settlement-event", target["id"], KALSHI + "/events/" + event_id)
                    try:
                        original = instant(rule_identity(market)["original_start"]).date()
                    except ValueError:
                        with self.store.transaction():
                            map_target(self.store.db, target["id"])
                        continue
                    # A bounded seven-day window includes ordinary 48-hour deferrals.
                    get(
                        "settlement-schedule",
                        target["id"],
                        MLB_SCHEDULE,
                        {
                            "sportId": 1,
                            "startDate": (original - timedelta(days=3)).isoformat(),
                            "endDate": (original + timedelta(days=3)).isoformat(),
                        },
                    )
                except HTTPFailure as exc:
                    if exc.status in (401, 403):
                        raise
                    errors.append({"ticker": value, "error": str(exc)[:300]})
                except (ValueError, CollectionError) as exc:
                    errors.append({"ticker": value, "error": str(exc)[:300]})
        except (ValueError, CollectionError) as exc:
            errors.append({"error": str(exc)[:300]})
        except BaseException:
            self.store.finish_run(
                run_id, "interrupted", {"status": "interrupted", "run_id": run_id}
            )
            raise
        states = dict(
            self.store.db.execute(
                "SELECT state,COUNT(*) FROM settlement_targets WHERE run_id=? GROUP BY state",
                (run_id,),
            )
        )
        outcome = (
            "partial"
            if errors or selection_truncated or states.get("pending")
            else "complete"
            if targets
            else "inconclusive"
        )
        result = {
            "status": outcome,
            "run_id": run_id,
            "targets": len(targets),
            "mapping_states": states,
            "selection_truncated": selection_truncated,
            "errors": errors,
            "scope": "bounded settlement evidence; quarantine is not a verified mapping",
        }
        self.store.finish_run(run_id, outcome, result)
        return result


def observation_evidence(db, version, *, first=False):
    """Evidence for this exact revision, never a target's creation-order rank."""
    observation = db.execute(
        "SELECT o.fetch_id,o.parser_version,f.seq,f.retrieved_at,r.id AS request_id,"
        "r.request_key,r.run_id,r.stage FROM settlement_observations o "
        "JOIN fetches f ON f.id=o.fetch_id JOIN requests r ON r.id=f.request_id "
        "WHERE o.version_id=? AND o.kind=? AND o.entity_key=? "
        "AND (? IS NULL OR o.fetch_id=?) ORDER BY f.seq DESC,o.parser_version DESC LIMIT 1",
        (
            version["id"],
            version["kind"],
            version["entity_key"],
            version["first_fetch_id"] if first else None,
            version["first_fetch_id"] if first else None,
        ),
    ).fetchone()
    result = {
        "status": "missing",
        "reason": "observation_missing",
        "version_id": version["id"],
        "observation": None,
        "target_status": "missing",
        "target_reason": "observation_missing",
        "id": None,
        "run_id": None,
        "ticker": None,
        "terms_fetch_id": None,
        "market_fetch_id": None,
        "event_fetch_id": None,
        "schedule_fetch_id": None,
        "mapping_version_id": None,
        "state": None,
    }
    if observation is None:
        return result
    result.update(status="available", reason=None, observation=dict(observation))
    # A result's direct market evidence is valid even if refresh context is missing.
    if version["kind"] == "kalshi":
        result["market_fetch_id"] = observation["fetch_id"]
    target = db.execute(
        "SELECT * FROM settlement_targets WHERE id=? AND run_id=?",
        (observation["request_key"], observation["run_id"]),
    ).fetchone()
    if target is None:
        result["target_reason"] = "target_missing"
        return result
    fetch_id = observation["fetch_id"]
    associated = {
        "kalshi": target["ticker"] == version["entity_key"]
        and target["market_fetch_id"] == fetch_id
        and observation["stage"] == "settlement-market",
        "mapping": target["ticker"] == version["entity_key"]
        and target["mapping_version_id"] == version["id"]
        and (target["schedule_fetch_id"] or target["event_fetch_id"] or target["market_fetch_id"])
        == fetch_id,
        "mlb": target["schedule_fetch_id"] == fetch_id
        and observation["stage"] == "settlement-schedule",
    }[version["kind"]]
    if not associated:
        result["target_reason"] = "target_association_missing"
        return result
    result.update(dict(target))
    missing = [
        key
        for key in ("terms_fetch_id", "market_fetch_id", "event_fetch_id", "schedule_fetch_id")
        if target[key] is None
    ]
    result.update(
        target_status="incomplete" if missing or target["state"] == "pending" else "available",
        target_reason="refresh_incomplete" if missing or target["state"] == "pending" else None,
        missing_fields=missing,
    )
    return result


def version_view(db, version):
    if version is None:
        return None
    return {
        **json.loads(version["data_json"]),
        "version_id": version["id"],
        "first_fetch_id": version["first_fetch_id"],
        "first_evidence": observation_evidence(db, version, first=True),
        "latest_evidence": observation_evidence(db, version),
    }


def inspect(store, *, target=None, limit=20, history=False):
    # Read-only CLI inspection may run alongside a writer. Keep all claims and
    # their evidence in one SQLite snapshot, without acquiring the writer lock.
    own_transaction = not store.db.in_transaction
    if own_transaction:
        store.db.execute("BEGIN")
    try:
        return inspect_snapshot(store, target=target, limit=limit, history=history)
    finally:
        if own_transaction:
            store.db.execute("ROLLBACK")


def inspect_snapshot(store, *, target=None, limit=20, history=False):
    require_schema(store)
    if not 1 <= limit <= 100:
        raise ValueError("inspection limit must be 1..100")
    if target:
        ticker(target)
    rows = store.db.execute(
        "SELECT v.*, f.retrieved_at,f.seq FROM settlement_versions v JOIN fetches f ON f.id=v.first_fetch_id "
        "WHERE v.kind='kalshi' AND (? IS NULL OR v.entity_key=?) "
        "AND (? OR NOT EXISTS (SELECT 1 FROM settlement_versions n WHERE n.kind=v.kind AND n.entity_key=v.entity_key AND n.revision>v.revision)) ORDER BY v.id DESC LIMIT ?",
        (target, target, history, limit),
    ).fetchall()
    records = []
    for row in rows:
        value = dict(row)
        value["data"] = json.loads(value.pop("data_json"))
        mapping = latest(store.db, "mapping", row["entity_key"])
        value["latest_mapping"] = version_view(store.db, mapping)
        value["mapping_status"] = "available" if mapping else "missing"
        game_id = value["latest_mapping"].get("game_id") if mapping else None
        mlb = latest(store.db, "mlb", game_id) if game_id else None
        value["latest_mlb_result"] = version_view(store.db, mlb)
        value["mlb_status"] = "available" if mlb else "missing"
        value["first_evidence"] = observation_evidence(store.db, row, first=True)
        value["latest_evidence"] = observation_evidence(store.db, row)
        value["observation_count"] = store.db.execute(
            "SELECT COUNT(*) FROM settlement_observations WHERE version_id=?", (row["id"],)
        ).fetchone()[0]
        records.append(value)
    return {
        "status": "complete",
        "schema_version": 2,
        "evidence_version": 2,
        "records": records,
        "limit": limit,
        "history": history,
    }
