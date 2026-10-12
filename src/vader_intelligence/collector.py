import json
import sqlite3
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .forecast.discovery import COLLECTION_PROTOCOL, STAGE_REASONS, DiscoveryOutcome, build_summary
from .normalize import decode, normalizer
from .provenance import RunProvenanceV1, json_text, source_hash
from .scope import eligible
from .storage import DiscoveryGateError, timestamp
from .transport import (
    KALSHI,
    MLB_SCHEDULE,
    CollectionError,
    DeadlineExceeded,
    HTTPFailure,
    PaginationError,
)


class Collector:
    def __init__(self, store, reader, config):
        self.store, self.reader, self.config = store, reader, config

    def market_pages(self, run_id, namespace="discovery", trace=None):
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
                    if trace is not None:
                        for entry in trace:
                            entry["counts"] = False
                    continue
                raise
            data = decode(response.body)
            following = data.get("cursor") or ""
            if trace is not None:
                trace.append(
                    {
                        "request_key": key,
                        "fetch_id": response.fetch_id,
                        "has_next": bool(following),
                        "counts": True,
                    }
                )
            for m in data["markets"]:
                if m["event_ticker"].startswith("KXMLBGAME-"):
                    markets[m["ticker"]] = (m, response.fetch_id)
                if len(markets) > self.config.max_markets:
                    raise PaginationError(
                        "market_limit_exceeded", "market limit exceeded; discovery incomplete"
                    )
            if not following:
                return markets
            if following == cursor or following in seen:
                raise PaginationError(
                    "cursor_repeated", "pagination cursor repeated; discovery incomplete"
                )
            seen.add(following)
            cursor = following
        raise PaginationError("page_limit_exceeded", "page limit exceeded; discovery incomplete")

    def schedule_window(self):
        day = self.reader.now().astimezone(ZoneInfo("America/New_York")).date()
        return {
            "sportId": 1,
            "startDate": (day - timedelta(days=2)).isoformat(),
            "endDate": (day + timedelta(days=self.config.lookahead_days)).isoformat(),
        }

    def schedule(self, run_id):
        response = self.reader.get(
            run_id, "schedule", str(uuid.uuid4()), MLB_SCHEDULE, self.schedule_window()
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
        two_phase = self.store.schema_version() >= 3
        if resume:
            row = self.store.db.execute("SELECT * FROM runs WHERE id=?", (resume,)).fetchone()
            if row is None or row["kind"] != kind or row["status"] == "complete":
                raise ValueError("resume requires an unfinished run of the same command")
            prov = json.loads(row["provenance_json"])
            if prov["configuration"] != asdict(self.config) or prov["code_hash"] != source_hash():
                raise ValueError(
                    "resume requires the original configuration and code; start a new run"
                )
            protocol = prov.get("tool_configuration", {}).get("collection_protocol")
            if two_phase and protocol != COLLECTION_PROTOCOL:
                raise ValueError(
                    "resume requires a two-phase run on schema 3; this run used the legacy "
                    "collection loop, start a new run"
                )
            if not two_phase and protocol is not None:
                raise ValueError("resume requires the archive schema the run was started on")
            self.store.recover(run_id)
        else:
            tool_configuration = {"method": "GET", "series": "KXMLBGAME", "book_depth": 0}
            if two_phase:
                tool_configuration["collection_protocol"] = COLLECTION_PROTOCOL
            self.store.start_run(
                RunProvenanceV1(
                    run_id,
                    kind,
                    source_hash(),
                    asdict(self.config),
                    tool_configuration=tool_configuration,
                ),
                timestamp(self.reader.now()),
            )
        if two_phase:
            return self._run_two_phase(kind, resume, run_id, session, started)
        return self._run_legacy(kind, resume, run_id, session, started)

    # ------------------------------------------------------------------ legacy
    # Schema 1 and 2 keep the reviewed interleaved loop exactly as before.

    def _run_legacy(self, kind, resume, run_id, session, started):
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

    # --------------------------------------------------------------- two-phase
    # Schema 3: discovery, then one immutable summary, then books (plan §3.3).

    def _run_two_phase(self, kind, resume, run_id, session, started):
        outcome = DiscoveryOutcome(kind=kind, resumed=bool(resume))
        decisions = []
        errors = []
        books_this_pass = 0
        fatal = False
        summary = None
        pass_id = pass_digest = None
        eligibility_after = self.store.db.execute(
            "SELECT COALESCE(MAX(id),0) FROM eligibility"
        ).fetchone()[0]
        fetch_seq_after = self.store.db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM fetches"
        ).fetchone()[0]
        mode = "forward_shadow" if self.reader.live and kind != "fixture" else "synthetic"

        def pass_summary():
            counts = summary["counts"] if summary else {"contracts": 0, "games": 0}
            return {
                "discovery_complete": bool(summary and summary["complete"]),
                "discovery_pass_id": pass_id,
                "discovery_digest": pass_digest,
                "discovery_reasons": summary["reasons"]
                if summary
                else ["discovery_summary_not_committed"],
                "eligible_contracts": counts["contracts"],
                "eligible_games": counts["games"],
                "eligibility_after_id": eligibility_after,
                "eligibility_through_id": self.store.db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM eligibility"
                ).fetchone()[0],
                "books_this_pass": books_this_pass,
            }

        def commit_summary(completed_at):
            # Last step before any book request. One BEGIN IMMEDIATE through the
            # journal; typed references are re-validated inside that transaction.
            created_at = timestamp(self.reader.now())
            if datetime.fromisoformat(created_at) < datetime.fromisoformat(completed_at):
                created_at = completed_at
            payload = build_summary(
                self.store.db,
                run_id=run_id,
                session_id=session,
                completed_at=completed_at,
                created_at=created_at,
                eligibility_after_id=eligibility_after,
                fetch_seq_after=fetch_seq_after,
                config=self.config,
                mode=mode,
                outcome=outcome,
            )
            identifier, digest = self.store.append_forecast("discovery_passes", payload)
            return payload, identifier, digest

        def finish(status):
            result = self.store.summary(run_id)
            result.update(
                status=status,
                errors=errors,
                **pass_summary(),
                elapsed_seconds=time.monotonic() - started,
            )
            self.store.finish_run(run_id, status, result)
            return result

        try:
            fatal = self._discover(run_id, session, resume, outcome, decisions)
        except BaseException:
            # Operator interrupt or other abnormal exit during Phase A: best-effort
            # incomplete summary, then the existing interrupted bookkeeping.
            outcome.fail("interrupted", outcome.phase_reached, session, "process interrupted")
            try:
                summary, pass_id, pass_digest = commit_summary(timestamp(self.reader.now()))
            except Exception as exc:  # the interrupt still propagates below
                errors.append(f"discovery_summary_not_committed: {str(exc)[:300]}")
            errors.append("process interrupted")
            finish("interrupted")
            raise
        completed_at = timestamp(self.reader.now())
        try:
            summary, pass_id, pass_digest = commit_summary(completed_at)
        except (ValueError, TypeError, KeyError, OSError, TimeoutError, sqlite3.Error) as exc:
            errors.append(f"discovery_summary_not_committed: {str(exc)[:300]}")
            return finish("failed")
        errors.extend(f"{f['stage']}:{f['key']}: {f['error']}" for f in outcome.failures)
        if not summary["complete"] and not errors:
            errors.append("discovery incomplete: " + ", ".join(summary["reasons"]))
        if fatal:
            return finish("failed")
        status = "partial" if errors else "complete"
        if summary["complete"] and kind != "discover":
            try:
                books_this_pass = self._books(run_id, session, decisions, errors)
            except (DiscoveryGateError, CollectionError, ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc))
                return finish("failed")
            except BaseException:
                errors.append("process interrupted")
                finish("interrupted")
                raise
            if errors:
                status = "partial"
        if kind != "discover" and status == "complete" and books_this_pass == 0:
            status = "inconclusive"
        return finish(status)

    def _discover(self, run_id, session, resume, outcome, decisions):
        """Phase A. Returns True when a stage failure ended the phase (run failed).

        Never requests a book. Every failure is recorded on the outcome with its
        fixed reason; only BaseException (interrupt) propagates.
        """
        outcome.started_at = timestamp(self.reader.now())
        stage = "status"
        try:
            outcome.use(self.reader.get(run_id, "status", session, KALSHI + "/exchange/status"))
            outcome.reached("status")
            stage = "series"
            series_response = outcome.use(
                self.reader.get(run_id, "series", session, KALSHI + "/series/KXMLBGAME")
            )
            series = decode(series_response.body)["series"]
            if series.get("ticker") != "KXMLBGAME":
                outcome.fail(
                    "series_identity_mismatch", stage, session, "unexpected series identity"
                )
                return True
            outcome.reached("series")
            stage = "documents"
            for field in ("contract_terms_url", "contract_url"):
                url = series.get(field)
                if not isinstance(url, str):
                    raise CollectionError(f"series missing {field}")
                document = outcome.use(
                    self.reader.get(
                        run_id,
                        "document",
                        session + ":" + url,
                        url,
                        apply=normalizer("document", url),
                    )
                )
                sha = self.store.db.execute(
                    "SELECT body_sha256 FROM fetches WHERE id=?", (document.fetch_id,)
                ).fetchone()[0]
                outcome.documents.append(
                    {"url": url, "fetch_id": document.fetch_id, "raw_sha256": sha}
                )
            outcome.reached("documents")
            stage = "pages"
            try:
                candidates = self.market_pages(run_id, trace=outcome.page_trace)
                if resume:
                    # Finish the checkpointed scan, then refresh the universe before using it.
                    outcome.page_trace.clear()
                    candidates = self.market_pages(
                        run_id, "refresh:" + session, trace=outcome.page_trace
                    )
            except PaginationError as exc:
                outcome.fail(exc.reason, stage, "markets", exc)
                return True
            for entry in outcome.page_trace:
                outcome.used_fetch_ids.add(entry["fetch_id"])
            outcome.candidates = {
                ticker: (market["event_ticker"], fetch_id)
                for ticker, (market, fetch_id) in candidates.items()
            }
            outcome.candidates_known = True
            outcome.reached("pages")
            stage = "schedule"
            schedule_response, schedule = self._schedule(run_id, outcome)
            outcome.reached("schedule")
            stage = "events"
            event_ids = sorted({m[0]["event_ticker"] for m in candidates.values()})
            for event_id in event_ids:
                record = {
                    "event_ticker": event_id,
                    "fetch_id": None,
                    "market_tickers": [],
                    "error": None,
                }
                outcome.events.append(record)
                try:
                    event_response = outcome.use(
                        self.reader.get(
                            run_id,
                            "event",
                            session + ":" + event_id,
                            KALSHI + "/events/" + event_id,
                        )
                    )
                    record["fetch_id"] = event_response.fetch_id
                    data = decode(event_response.body)
                    event, event_markets = data["event"], data["markets"]
                    record["market_tickers"] = sorted(
                        str(m.get("ticker")) for m in event_markets if isinstance(m, dict)
                    )
                    if event.get("event_ticker") != event_id:
                        record["error"] = "event identity mismatch"
                        outcome.fail("event_identity_mismatch", stage, event_id, record["error"])
                        continue
                    if len(event_markets) != 2:
                        record["error"] = "event does not contain exactly two team contracts"
                        outcome.fail("event_membership_ambiguous", stage, event_id, record["error"])
                        continue
                except HTTPFailure as exc:
                    if exc.status in (401, 403):
                        raise
                    record["error"] = str(exc)
                    outcome.fail("event_unavailable", stage, event_id, exc)
                    continue
                except DeadlineExceeded as exc:
                    record["error"] = str(exc)
                    raise
                except (CollectionError, ValueError, TypeError, KeyError) as exc:
                    record["error"] = str(exc)
                    outcome.fail("event_unavailable", stage, event_id, exc)
                    continue
                outcome.reached("events")
                for market in event_markets:
                    if market["ticker"] not in candidates:
                        continue
                    now = self.reader.now()
                    age = (
                        now - datetime.fromisoformat(schedule_response.retrieved_at)
                    ).total_seconds()
                    if age < 0 or age > self.config.schedule_max_age - 5:
                        stage = "schedule"
                        schedule_response, schedule = self._schedule(run_id, outcome)
                        stage = "events"
                        now = self.reader.now()
                    outcome.eligibility_started = True
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
                    decisions.append(
                        {
                            "ticker": market["ticker"],
                            "market": market,
                            "event": event,
                            "event_markets": event_markets,
                            "event_response": event_response,
                            "schedule_response": schedule_response,
                            "check": check,
                        }
                    )
                    outcome.reached("eligibility")
            # "done" means every discovered candidate has a decision and nothing failed.
            if not outcome.reasons and set(candidates) <= {d["ticker"] for d in decisions}:
                outcome.reached("done")
            return False
        except HTTPFailure as exc:
            if exc.status in (401, 403):
                outcome.fail("access_denied", stage, session, exc)
            else:
                outcome.fail(STAGE_REASONS[stage], stage, session, exc)
            return True
        except DeadlineExceeded as exc:
            outcome.fail("deadline_exhausted", stage, session, exc)
            return True
        except (CollectionError, ValueError, TypeError, KeyError) as exc:
            outcome.fail(STAGE_REASONS[stage], stage, session, exc)
            return True

    def _schedule(self, run_id, outcome):
        window = self.schedule_window()
        if outcome.request is None:
            outcome.request = {
                "series_ticker": "KXMLBGAME",
                "status": "open",
                "schedule_window": {"start": window["startDate"], "end": window["endDate"]},
            }
        response, schedule = self.schedule(run_id)
        outcome.use(response)
        outcome.schedule_fetch_ids.append(response.fetch_id)
        for date in schedule.get("dates", []):
            for game in date.get("games", []) if isinstance(date, dict) else []:
                if isinstance(game, dict) and isinstance(game.get("gamePk"), int):
                    outcome.official_game_ids.add(game["gamePk"])
        return response, schedule

    def _books(self, run_id, session, decisions, errors):
        """Phase B. Runs only after a complete summary has been committed.

        Same contract order as the legacy loop. The persistence boundary in
        Store.request independently refuses any book without that summary.
        """
        books_this_pass = 0
        current = None  # (response, schedule) fetched during Phase B, if any
        for decision in decisions:
            if not decision["check"].eligible:
                continue
            market, check = decision["market"], decision["check"]
            schedule_response = decision["schedule_response"]
            try:
                now = self.reader.now()
                age = (now - datetime.fromisoformat(schedule_response.retrieved_at)).total_seconds()
                if age < 0 or age > self.config.schedule_max_age - 5:
                    # Renewed book-specific proof: a new eligibility row after the
                    # summary bounds. The summary itself is never changed.
                    if current is not None:
                        current_age = (
                            now - datetime.fromisoformat(current[0].retrieved_at)
                        ).total_seconds()
                    if (
                        current is None
                        or current_age < 0
                        or current_age > (self.config.schedule_max_age - 5)
                    ):
                        current = self.schedule(run_id)
                        now = self.reader.now()
                    check = eligible(
                        market,
                        decision["event"],
                        decision["event_markets"],
                        current[1],
                        current[0].retrieved_at,
                        now,
                        self.config,
                    )
                    self.record_eligibility(
                        run_id,
                        market["ticker"],
                        check,
                        current[0].fetch_id,
                        decision["event_response"].fetch_id,
                    )
                    schedule_response = current[0]
                    if not check.eligible:
                        continue
                cutoff = datetime.fromisoformat(check.cutoff)
                budget = (cutoff - self.reader.now()).total_seconds()
                if budget <= 0:
                    continue

                def check_before_attempt(schedule_response=schedule_response, cutoff=cutoff):
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
            except DiscoveryGateError:
                raise
            except HTTPFailure as exc:
                if exc.status in (401, 403):
                    raise
                errors.append(str(exc))
            except (CollectionError, ValueError, TypeError, KeyError) as exc:
                errors.append(f"{market['ticker']}: {exc}")
        return books_this_pass
