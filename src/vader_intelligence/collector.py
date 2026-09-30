import json
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .normalize import decode, normalizer
from .provenance import RunProvenanceV1, json_text, source_hash
from .scope import eligible
from .storage import timestamp
from .transport import KALSHI, MLB_SCHEDULE, CollectionError, HTTPFailure


class Collector:
    def __init__(self, store, reader, config):
        self.store, self.reader, self.config = store, reader, config

    def market_pages(self, run_id, namespace="discovery"):
        cursor = ""
        seen = set()
        markets = {}
        restarted = False
        for page in range(self.config.max_pages):
            params = {
                "series_ticker": "KXMLBGAME",
                "status": "open",
                "limit": self.config.page_size,
            }
            if cursor:
                params["cursor"] = cursor
            key = namespace + ":" + ("restart:" if restarted else "") + cursor
            try:
                response = self.reader.get(run_id, "markets", key, KALSHI + "/markets", params)
            except HTTPFailure as exc:
                if exc.status == 400 and cursor and not restarted:
                    cursor, seen, markets, restarted = "", set(), {}, True
                    continue
                raise
            data = decode(response.body)
            for m in data["markets"]:
                if m["event_ticker"].startswith("KXMLBGAME-"):
                    markets[m["ticker"]] = (m, response.fetch_id)
                if len(markets) > self.config.max_markets:
                    raise CollectionError("market limit exceeded; discovery incomplete")
            following = data.get("cursor") or ""
            if not following:
                return markets
            if following == cursor or following in seen:
                raise CollectionError("pagination cursor repeated; discovery incomplete")
            seen.add(following)
            cursor = following
        raise CollectionError("page limit exceeded; discovery incomplete")

    def schedule(self, run_id):
        day = self.reader.now().astimezone(ZoneInfo("America/New_York")).date()
        response = self.reader.get(
            run_id,
            "schedule",
            str(uuid.uuid4()),
            MLB_SCHEDULE,
            {
                "sportId": 1,
                "startDate": (day - timedelta(days=2)).isoformat(),
                "endDate": (day + timedelta(days=self.config.lookahead_days)).isoformat(),
            },
        )
        return response, decode(response.body)

    def record_eligibility(self, run_id, ticker, check, schedule_id, market_id):
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO eligibility(run_id,ticker,checked_at,schedule_fetch_id,market_fetch_id,"
                "game_id,eligible,reason,cutoff,data_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    ticker,
                    timestamp(self.reader.now()),
                    schedule_id,
                    market_id,
                    check.game_id,
                    int(check.eligible),
                    check.reason,
                    check.cutoff,
                    json_text(check.to_dict()),
                ),
            )

    def run(self, *, kind="collect", resume=None):
        started = time.monotonic()
        run_id = resume or str(uuid.uuid4())
        session = str(uuid.uuid4())
        if resume:
            row = self.store.db.execute("SELECT * FROM runs WHERE id=?", (resume,)).fetchone()
            if row is None or row["kind"] != kind or row["status"] == "complete":
                raise ValueError("resume requires an unfinished run of the same command")
            prov = json.loads(row["provenance_json"])
            if prov["configuration"] != asdict(self.config) or prov["code_hash"] != source_hash():
                raise ValueError(
                    "resume requires the original configuration and code; start a new run"
                )
            self.store.recover(run_id)
        else:
            self.store.start_run(
                RunProvenanceV1(
                    run_id,
                    kind,
                    source_hash(),
                    asdict(self.config),
                    tool_configuration={"method": "GET", "series": "KXMLBGAME", "book_depth": 0},
                ),
                timestamp(self.reader.now()),
            )
        errors = []
        status = "complete"
        books_this_pass = 0
        eligibility_after = self.store.db.execute(
            "SELECT COALESCE(MAX(id),0) FROM eligibility"
        ).fetchone()[0]
        checked, eligible_contracts, eligible_games = set(), set(), set()
        discovery_complete = False

        def pass_summary():
            return {
                "discovery_complete": discovery_complete,
                "eligible_contracts": len(eligible_contracts),
                "eligible_games": len(eligible_games),
                "eligibility_after_id": eligibility_after,
                "eligibility_through_id": self.store.db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM eligibility"
                ).fetchone()[0],
                "books_this_pass": books_this_pass,
            }

        try:
            self.reader.get(run_id, "status", session, KALSHI + "/exchange/status")
            series_response = self.reader.get(
                run_id, "series", session, KALSHI + "/series/KXMLBGAME"
            )
            series = decode(series_response.body)["series"]
            if series.get("ticker") != "KXMLBGAME":
                raise CollectionError("unexpected series identity")
            for field in ("contract_terms_url", "contract_url"):
                url = series.get(field)
                if not isinstance(url, str):
                    raise CollectionError(f"series missing {field}")
                self.reader.get(
                    run_id, "document", session + ":" + url, url, apply=normalizer("document", url)
                )
            candidates = self.market_pages(run_id)
            if resume:
                # Finish the checkpointed scan, then refresh the universe before using it.
                candidates = self.market_pages(run_id, "refresh:" + session)
            schedule_response, schedule = self.schedule(run_id)
            event_ids = sorted({m[0]["event_ticker"] for m in candidates.values()})
            for event_id in event_ids:
                try:
                    event_response = self.reader.get(
                        run_id,
                        "event",
                        session + ":" + event_id,
                        KALSHI + "/events/" + event_id,
                    )
                    data = decode(event_response.body)
                    event, event_markets = data["event"], data["markets"]
                    if event.get("event_ticker") != event_id:
                        raise CollectionError("event identity mismatch")
                    if len(event_markets) != 2:
                        raise CollectionError("event does not contain exactly two team contracts")
                    for market in event_markets:
                        if market["ticker"] not in candidates:
                            continue
                        now = self.reader.now()
                        age = (
                            now - datetime.fromisoformat(schedule_response.retrieved_at)
                        ).total_seconds()
                        if age < 0 or age > self.config.schedule_max_age - 5:
                            schedule_response, schedule = self.schedule(run_id)
                            now = self.reader.now()
                        check = eligible(
                            market,
                            event,
                            event_markets,
                            schedule,
                            schedule_response.retrieved_at,
                            now,
                            self.config,
                        )
                        self.record_eligibility(
                            run_id,
                            market["ticker"],
                            check,
                            schedule_response.fetch_id,
                            event_response.fetch_id,
                        )
                        checked.add(market["ticker"])
                        if check.eligible:
                            eligible_contracts.add(market["ticker"])
                            eligible_games.add(check.game_id)
                        if not check.eligible or kind == "discover":
                            continue
                        cutoff = datetime.fromisoformat(check.cutoff)
                        budget = (cutoff - self.reader.now()).total_seconds()
                        if budget <= 0:
                            continue

                        def check_before_attempt():
                            current = self.reader.now()
                            age = (
                                current - datetime.fromisoformat(schedule_response.retrieved_at)
                            ).total_seconds()
                            if current >= cutoff:
                                raise CollectionError("book request reached pregame cutoff")
                            if not 0 <= age <= self.config.schedule_max_age:
                                raise CollectionError("book request has stale schedule evidence")

                        self.reader.get(
                            run_id,
                            "book",
                            session + ":" + market["ticker"],
                            KALSHI + "/markets/" + market["ticker"] + "/orderbook",
                            {"depth": 0},
                            apply=normalizer("book", market["ticker"]),
                            budget=budget,
                            before_attempt=check_before_attempt,
                        )
                        books_this_pass += 1
                except HTTPFailure as exc:
                    if exc.status in (401, 403):
                        raise
                    errors.append(str(exc))
                except (CollectionError, ValueError, TypeError, KeyError) as exc:
                    errors.append(f"{event_id}: {exc}")
            discovery_complete = checked == set(candidates)
            if not discovery_complete and not errors:
                errors.append(
                    "candidate eligibility incomplete; event response omitted discovered contracts"
                )
            if errors:
                status = "partial"
        except (CollectionError, ValueError, TypeError, KeyError) as exc:
            errors.append(str(exc))
            status = "failed"
        except BaseException:
            result = self.store.summary(run_id)
            result.update(
                status="interrupted",
                errors=["process interrupted"],
                elapsed_seconds=time.monotonic() - started,
                **pass_summary(),
            )
            self.store.finish_run(run_id, "interrupted", result)
            raise
        result = self.store.summary(run_id)
        if kind != "discover" and status == "complete" and books_this_pass == 0:
            status = "inconclusive"
        result.update(
            status=status,
            errors=errors,
            **pass_summary(),
            elapsed_seconds=time.monotonic() - started,
        )
        self.store.finish_run(run_id, status, result)
        return result
