"""Bounded, resumable acquisition of public historical MLB market data (roadmap A4).

A separate source from the collector, for research datasets only. It reads Kalshi's
public historical tier and settled-market listings, the official MLB schedule, and
1-minute candlesticks ending at each identified game's T-60 cutoff, through the existing
archiving reader (every attempt has its own retrieval identity, raw bytes, SHA-256 and
retrieval time; GET only; 2 requests/s; bounded retries) with the `history` source
profile. Runs are kind `historical-acquire`; their provenance records the interpretation
version and retrieval kind `historical`. Nothing acquired here is forward evidence: the
forecast tick never reads these stages, and datasets label them historical retrieval.
"""

import time
import uuid
from datetime import date, datetime, timedelta, timezone

from ..normalize import decode
from ..provenance import RunProvenanceV1, json_text, source_hash
from ..storage import timestamp
from ..transport import KALSHI, MLB_SCHEDULE, CollectionError
from . import interpret

RUN_KIND = "historical-acquire"
MAX_RANGE_DAYS = 250
LISTINGS = (
    ("hist-markets", KALSHI + "/historical/markets", {}),
    ("hist-settled", KALSHI + "/markets", {"status": "settled"}),
)


def _page_validator(db, fetch_id, body):
    data = decode(body)
    markets = data.get("markets")
    cursor = data.get("cursor")
    if not isinstance(markets, list) or len(markets) > 1000:
        raise ValueError("invalid market page")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise ValueError("invalid pagination cursor")
    return {"cursor": cursor or "", "markets": len(markets)}


def _json_validator(key):
    def apply(db, fetch_id, body):
        data = decode(body)
        if not isinstance(data, dict) or (key and key not in data):
            raise ValueError(f"expected an object with {key!r}")
        return {}

    return apply


def _ticker_date(ticker):
    from ..scope import ticker_identity

    found = ticker_identity(ticker.rsplit("-", 1)[0]) if isinstance(ticker, str) else None
    return None if found is None else found["start"].date()


class Acquisition:
    def __init__(self, store, reader, *, max_requests=8000, budget=7200.0, monotonic=None):
        if getattr(reader, "profile", None) != "history":
            raise ValueError("historical acquisition requires a reader with profile='history'")
        if type(max_requests) is not int or not 1 <= max_requests <= 50000:
            raise ValueError("max_requests must be an integer in [1, 50000]")
        if not isinstance(budget, (int, float)) or not 0 < budget <= 6 * 3600:
            raise ValueError("budget must be in (0, 21600] seconds")
        self.store, self.reader = store, reader
        self.max_requests, self.budget = max_requests, budget
        self.monotonic = monotonic or time.monotonic

    # ------------------------------------------------------------------ helpers
    def _get(self, run_id, stage, key, url, params, apply):
        done = self.store.db.execute(
            "SELECT done FROM requests WHERE run_id=? AND stage=? AND request_key=?",
            (run_id, stage, key),
        ).fetchone()
        if not (done and done[0]):
            if self.made >= self.max_requests:
                raise _Bound("request_limit")
            if self.monotonic() >= self.deadline:
                raise _Bound("time_budget")
            self.made += 1
        return self.reader.get(run_id, stage, key, url, params, apply=apply)

    def run(
        self,
        *,
        start_date,
        through_date,
        resume=None,
        window_seconds=3600,
        period_minutes=1,
        page_size=200,
        max_pages=200,
    ):
        if not isinstance(start_date, date) or not isinstance(through_date, date):
            raise ValueError("dates required")
        if not 0 <= (through_date - start_date).days < MAX_RANGE_DAYS:
            raise ValueError(f"range must be 1..{MAX_RANGE_DAYS} days")
        if type(window_seconds) is not int or not 60 <= window_seconds <= 6 * 3600:
            raise ValueError("window_seconds must be an integer in [60, 21600]")
        if period_minutes not in (1, 60):
            raise ValueError("period_minutes must be 1 or 60")
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be in [1, 1000]")
        configuration = {
            "start_date": start_date.isoformat(),
            "through_date": through_date.isoformat(),
            "window_seconds": window_seconds,
            "period_minutes": period_minutes,
            "page_size": page_size,
            "max_pages": max_pages,
            "interpretation": interpret.INTERPRETATION,
        }
        tool = {
            "source_lineage": "live" if self.reader.live else "synthetic",
            "retrieval_kind": "historical",
            "reader_profile": "history",
            "interpretation": interpret.INTERPRETATION,
        }
        db = self.store.db
        if resume:
            row = db.execute(
                "SELECT kind, status, provenance_json FROM runs WHERE id=?", (resume,)
            ).fetchone()
            if row is None or row[0] != RUN_KIND or row[1] == "complete":
                raise ValueError("resume requires an unfinished historical-acquire run")
            import json

            recorded = json.loads(row[2])
            if (
                recorded["configuration"] != configuration
                or recorded["code_hash"] != source_hash()
                or recorded.get("tool_configuration") != tool
            ):
                raise ValueError("resume requires the original configuration, code and reader")
            run_id = resume
            self.store.recover(run_id)
        else:
            run_id = str(uuid.uuid4())
            self.store.start_run(
                RunProvenanceV1(
                    run_id, RUN_KIND, source_hash(), configuration, tool_configuration=tool
                ),
                timestamp(self.reader.now()),
            )
        self.made = 0
        self.deadline = self.monotonic() + self.budget
        summary = {
            "run_id": run_id,
            "evidence_kind": "historical_retrieval",
            "configuration": configuration,
            "requests_made": 0,
            "pages": {},
            "listing_complete": {},
            "markets_in_range": 0,
            "events_in_range": 0,
            "identified_events": 0,
            "unresolved_by_reason": {},
            "candle_requests": {"ok": 0, "failed": 0},
            "errors": [],
        }
        status = "complete"
        try:
            self._get(
                run_id,
                "hist-cutoff",
                "cutoff",
                KALSHI + "/historical/cutoff",
                {},
                _json_validator("market_settled_ts"),
            )
            for stage, url, extra in LISTINGS:
                cursor, pages = "", 0
                while True:
                    if pages >= max_pages:
                        summary["listing_complete"][stage] = False
                        status = "partial"
                        break
                    params = {"series_ticker": "KXMLBGAME", "limit": page_size} | extra
                    if cursor:
                        params["cursor"] = cursor
                    response = self._get(
                        run_id, stage, f"page:{pages}", url, params, _page_validator
                    )
                    pages += 1
                    data = decode(response.body)
                    cursor = data.get("cursor") or ""
                    if not cursor or not data.get("markets"):
                        summary["listing_complete"][stage] = True
                        break
                summary["pages"][stage] = pages
            day = start_date - timedelta(days=1)
            last = through_date + timedelta(days=1)
            while day <= last:
                end = min(day + timedelta(days=6), last)
                self._get(
                    run_id,
                    "hist-schedule",
                    f"schedule:{day.isoformat()}:{end.isoformat()}",
                    MLB_SCHEDULE,
                    {"sportId": 1, "startDate": day.isoformat(), "endDate": end.isoformat()},
                    _json_validator("dates"),
                )
                day = end + timedelta(days=1)
            records = {
                t: m
                for t, m in interpret.markets(db, run_id).items()
                if (d := _ticker_date(t)) is not None and start_date <= d <= through_date
            }
            games = interpret.schedule(db, run_id)
            grouped = interpret.events(records)
            summary["markets_in_range"] = len(records)
            summary["events_in_range"] = len(grouped)
            for event_ticker, event_markets in sorted(grouped.items()):
                identity = interpret.identify(event_markets, games)
                if identity["status"] != "identified":
                    reason = identity["reason"]
                    summary["unresolved_by_reason"][reason] = (
                        summary["unresolved_by_reason"].get(reason, 0) + 1
                    )
                    continue
                summary["identified_events"] += 1
                cutoff = datetime.fromisoformat(identity["start"]) - timedelta(hours=1)
                end_ts = int(cutoff.astimezone(timezone.utc).timestamp())
                start_ts = end_ts - window_seconds
                for market in event_markets:
                    ticker = market["ticker"]
                    url = (
                        f"{KALSHI}/historical/markets/{ticker}/candlesticks"
                        if market["source"] == "historical"
                        else f"{KALSHI}/series/KXMLBGAME/markets/{ticker}/candlesticks"
                    )
                    try:
                        self._get(
                            run_id,
                            "hist-candles",
                            f"candles:{ticker}:{start_ts}:{end_ts}:{period_minutes}",
                            url,
                            {
                                "start_ts": start_ts,
                                "end_ts": end_ts,
                                "period_interval": period_minutes,
                            },
                            _json_validator("candlesticks"),
                        )
                        summary["candle_requests"]["ok"] += 1
                    except CollectionError as exc:
                        # One market's candles failing is recorded and the run goes on;
                        # the dataset reports the missing input, nothing is fabricated.
                        summary["candle_requests"]["failed"] += 1
                        if len(summary["errors"]) < 100:
                            summary["errors"].append(f"{ticker}: {str(exc)[:200]}")
        except _Bound as bound:
            status = "partial"
            summary["errors"].append(f"bound reached: {bound.reason}; resume with this run id")
        except (CollectionError, ValueError) as exc:
            status = "failed"
            summary["errors"].append(str(exc)[:300])
        except BaseException:
            self.store.finish_run(run_id, "interrupted", summary | {"status": "interrupted"})
            raise
        if status == "complete" and summary["candle_requests"]["failed"]:
            status = "partial"
        summary["requests_made"] = self.made
        summary["status"] = status
        self.store.finish_run(run_id, status, json_text_safe(summary))
        return summary


class _Bound(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def json_text_safe(value):
    """Summary dicts are JSON-native; round-trip guards against accidental non-JSON."""
    import json

    return json.loads(json_text(value))
