"""Historical acquisition (roadmap A4): synthetic transport only, no network.

The payload shapes follow Kalshi's public historical and settled-market listings and
both candlestick field variants as observed in a bounded live probe on 2026-10-19; the
values here are invented.
"""

import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import Clock, response

from vader_intelligence.cli import execute, parser
from vader_intelligence.history import interpret
from vader_intelligence.history.acquire import Acquisition
from vader_intelligence.storage import Store
from vader_intelligence.transport import KALSHI, Reader, validate_source

DAY = "2026-09-20"


def market(ticker, winner, away, home, *, timed=True, result="yes"):
    when = " at 1:10 PM EDT" if timed else ""
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "market_type": "binary",
        "notional_value_dollars": "1.0000",
        "yes_sub_title": winner,
        "rules_primary": f"If {winner} wins the {away} vs {home} professional baseball game"
        f" originally scheduled for Sep 20, 2026{when}, then the market resolves to Yes.",
        "status": "finalized",
        "result": result,
        "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000",
        "settlement_ts": "2026-09-20T20:36:24Z",
    }


def pair(event, away, home, away_code, home_code, *, timed=True, winner_is_home=True):
    a, h = f"{event}-{away_code}", f"{event}-{home_code}"
    return [
        market(a, away, away, home, timed=timed, result="no" if winner_is_home else "yes"),
        market(h, home, away, home, timed=timed, result="yes" if winner_is_home else "no"),
    ]


HISTORICAL = pair("KXMLBGAME-26SEP201310PHINYM", "Philadelphia", "New York M", "PHI", "NYM")
SETTLED = (
    pair("KXMLBGAME-26SEP201610HOUATH", "Houston", "A's", "HOU", "ATH", timed=False)
    + pair("KXMLBGAME-26SEP201905BOSNYY", "Boston", "New York Y", "BOS", "NYY")
    + pair("KXMLBGAME-26SEP251310CLETB", "Cleveland", "Tampa Bay", "CLE", "TB")  # out of range
)


def game(pk, away_id, home_id, start, official=DAY):
    return {
        "gamePk": pk,
        "gameDate": start,
        "officialDate": official,
        "gameType": "R",
        "gameNumber": 1,
        "doubleHeader": "N",
        "status": {"abstractGameState": "Final", "detailedState": "Final", "startTimeTBD": False},
        "teams": {
            "away": {"team": {"id": away_id}, "score": 2, "isWinner": False},
            "home": {"team": {"id": home_id}, "score": 5, "isWinner": True},
        },
    }


SCHEDULE = {
    "dates": [
        {
            "date": DAY,
            "games": [
                game(900001, 143, 121, "2026-09-20T17:10:00Z"),  # PHI @ NYM, as stated
                game(900002, 117, 133, "2026-09-20T20:10:00Z"),  # HOU @ ATH, ticker 16:10 ET
                game(900003, 111, 147, "2026-09-20T23:35:00Z"),  # BOS @ NYY moved: excluded
            ],
        }
    ]
}


def candle_live(end):
    return {
        "end_period_ts": end,
        "yes_bid": {"close_dollars": "0.4000"},
        "yes_ask": {"close_dollars": "0.4200"},
        "price": {"close_dollars": "0.4100", "mean_dollars": "0.4100"},
        "volume_fp": "12.00",
        "open_interest_fp": "100.00",
    }


def candle_historical(end):
    return {
        "end_period_ts": end,
        "yes_bid": {"close": "0.6000"},
        "yes_ask": {"close": "0.6100"},
        "price": {"close": "0.6050", "mean": "0.6050"},
        "volume": "3.00",
        "open_interest": "50.00",
    }


def handler_factory(
    calls, *, page_size_pages=2, fail_ticker=None, schedule=None, unavailable_ticker=None
):
    def handler(request):
        calls.append(request)
        path = request.url.path.removeprefix("/trade-api/v2")
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if path == "/historical/cutoff":
            return response(data={"market_settled_ts": "2026-09-19T00:00:00Z"})
        if path == "/historical/markets":
            if query.get("cursor") == "c1":
                return response(data={"markets": HISTORICAL[1:], "cursor": ""})
            return response(data={"markets": HISTORICAL[:1], "cursor": "c1"})
        if path == "/markets":
            assert query["status"] == "settled"
            return response(data={"markets": SETTLED, "cursor": ""})
        if request.url.host == "statsapi.mlb.com":
            return response(data=schedule or SCHEDULE)
        if path.endswith("/candlesticks"):
            ticker = path.split("/")[-2]
            if ticker == fail_ticker:
                return response(status=404, data={"error": "not found"})
            if ticker == unavailable_ticker:
                return response(status=503, data={"error": "unavailable"})
            end, start = int(query["end_ts"]), int(query["start_ts"])
            maker = candle_historical if path.startswith("/historical/") else candle_live
            return response(
                data={"ticker": ticker, "candlesticks": [maker(start + 60), maker(end)]}
            )
        raise AssertionError(f"unexpected request {request.url}")

    return handler


def history_reader(store, config, handler, clock=None):
    clock = clock or Clock()
    clock.origin = datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc)
    return Reader(
        store,
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=clock.now,
        monotonic=clock.mono,
        sleep=clock.sleep,
        jitter=lambda a, b: b,
        profile="history",
    )


def acquire(store, config, calls, **kwargs):
    reader = history_reader(store, config, handler_factory(calls, **kwargs.pop("handler", {})))
    try:
        return Acquisition(store, reader, **kwargs).run(
            start_date=date(2026, 9, 20), through_date=date(2026, 9, 20)
        )
    finally:
        reader.close()


def test_acquisition_archives_identifies_and_chooses_the_candle_endpoint(store, config):
    calls = []
    result = acquire(store, config, calls)
    assert result["status"] == "complete", result
    assert result["evidence_kind"] == "historical_retrieval"
    assert result["markets_in_range"] == 6 and result["events_in_range"] == 3
    assert result["identified_events"] == 2
    assert result["unresolved_by_reason"] == {"start_differs": 1}
    assert result["listing_complete"] == {"hist-markets": True, "hist-settled": True}
    candle_paths = sorted(r.url.path for r in calls if r.url.path.endswith("/candlesticks"))
    assert candle_paths == [
        "/trade-api/v2/historical/markets/KXMLBGAME-26SEP201310PHINYM-NYM/candlesticks",
        "/trade-api/v2/historical/markets/KXMLBGAME-26SEP201310PHINYM-PHI/candlesticks",
        "/trade-api/v2/series/KXMLBGAME/markets/KXMLBGAME-26SEP201610HOUATH-ATH/candlesticks",
        "/trade-api/v2/series/KXMLBGAME/markets/KXMLBGAME-26SEP201610HOUATH-HOU/candlesticks",
    ]
    # The candle window ends exactly at C = official start - 3600 s.
    phi = next(r for r in calls if "PHINYM-PHI/candlesticks" in r.url.path)
    end_ts = int(parse_qs(phi.url.query.decode())["end_ts"][0])
    assert datetime.fromtimestamp(end_ts, timezone.utc).isoformat() == "2026-09-20T16:10:00+00:00"
    run = store.db.execute("SELECT kind, provenance_json FROM runs").fetchone()
    tool = json.loads(run[1])["tool_configuration"]
    assert run[0] == "historical-acquire"
    assert tool == {
        "source_lineage": "synthetic",
        "retrieval_kind": "historical",
        "reader_profile": "history",
        "interpretation": "history-v1",
    }
    # Every retrieval keeps its own identity and raw bytes.
    assert store.verify_integrity() == []
    assert store.db.execute("SELECT COUNT(*) FROM fetches WHERE state='ok'").fetchone()[0] == len(
        calls
    )


def test_interpretation_reads_both_candle_variants_and_the_identity(store, config):
    acquire(store, config, [])
    db = store.db
    historical = interpret.candles(db, "KXMLBGAME-26SEP201310PHINYM-PHI")
    live = interpret.candles(db, "KXMLBGAME-26SEP201610HOUATH-HOU")
    assert [c["yes_bid_close"] for c in historical] == ["0.6000", "0.6000"]
    assert [c["price_mean"] for c in live] == ["0.4100", "0.4100"]
    assert historical[-1]["end"].isoformat() == "2026-09-20T16:10:00+00:00"
    assert all(c["fetch_id"] for c in historical + live)
    records = interpret.markets(db)
    games = interpret.schedule(db)
    events = interpret.events(records)
    v1 = interpret.identify(events["KXMLBGAME-26SEP201310PHINYM"], games)
    assert v1["status"] == "identified" and v1["rule_version"] == 1 and v1["game_id"] == 900001
    assert v1["selected_ticker"].endswith("-NYM")  # lower team id (NYM 121 < PHI 143)
    v2 = interpret.identify(events["KXMLBGAME-26SEP201610HOUATH"], games)
    assert v2["status"] == "identified" and v2["rule_version"] == 2
    assert v2["start"] == "2026-09-20T20:10:00+00:00"
    moved = interpret.identify(events["KXMLBGAME-26SEP201905BOSNYY"], games)
    assert moved == {"status": "unresolved", "reason": "start_differs"}
    # Whole archive, any date: the out-of-range event has no schedule entry here.
    summary = interpret.summary(db)
    assert summary["identified_events"] == 2
    assert summary["unresolved_by_reason"] == {"start_differs": 1, "unmapped_game": 1}


def test_bound_then_resume_completes_without_repeating_requests(store, config):
    calls = []
    first = acquire(store, config, calls, max_requests=5)
    assert first["status"] == "partial" and first["requests_made"] == 5
    assert "request_limit" in first["errors"][0]
    reader = history_reader(store, config, handler_factory(calls))
    try:
        second = Acquisition(store, reader).run(
            start_date=date(2026, 9, 20), through_date=date(2026, 9, 20), resume=first["run_id"]
        )
    finally:
        reader.close()
    assert second["status"] == "complete" and second["run_id"] == first["run_id"]
    assert second["requests_made"] == len(calls) - 5
    duplicates = store.db.execute(
        "SELECT request_id, COUNT(*) FROM fetches GROUP BY request_id HAVING COUNT(*) > 1"
    ).fetchall()
    assert duplicates == []


def test_resume_refuses_a_changed_configuration(store, config):
    first = acquire(store, config, [], max_requests=3)
    reader = history_reader(store, config, handler_factory([]))
    try:
        with pytest.raises(ValueError, match="original configuration"):
            Acquisition(store, reader).run(
                start_date=date(2026, 9, 20),
                through_date=date(2026, 9, 21),
                resume=first["run_id"],
            )
    finally:
        reader.close()


def test_a_failed_candle_request_is_recorded_and_the_run_is_partial(store, config):
    result = acquire(store, config, [], handler={"fail_ticker": "KXMLBGAME-26SEP201610HOUATH-ATH"})
    assert result["status"] == "partial"
    assert result["candle_requests"] == {"ok": 3, "failed": 1}
    assert "KXMLBGAME-26SEP201610HOUATH-ATH" in result["errors"][0]


def test_source_profiles_keep_the_collector_allowlist_unchanged():
    candles = KALSHI + "/historical/markets/KXMLBGAME-26SEP201310PHINYM-PHI/candlesticks"
    params = {"start_ts": 1, "end_ts": 2, "period_interval": 1}
    assert validate_source(candles, params, "history") == "kalshi"
    with pytest.raises(ValueError):
        validate_source(candles, params)  # collector profile
    with pytest.raises(ValueError, match="settled"):
        validate_source(KALSHI + "/markets", {"series_ticker": "KXMLBGAME"}, "history")
    for url, query in (
        (KALSHI + "/portfolio/orders", {}),
        (KALSHI + "/historical/fills", {}),
        (KALSHI + "/historical/markets/KXNBAGAME-X/candlesticks", params),
        (candles, params | {"include_latest_before_start": "true"}),
    ):
        with pytest.raises(ValueError):
            validate_source(url, query, "history")
    with pytest.raises(ValueError, match="profile"):
        validate_source(candles, params, "trading")


def test_acquisition_requires_a_history_reader(store, config):
    reader = Reader(store, config, client=httpx.Client(transport=httpx.MockTransport(lambda r: 0)))
    try:
        with pytest.raises(ValueError, match="profile='history'"):
            Acquisition(store, reader)
    finally:
        reader.close()


def test_cli_refuses_an_archive_that_holds_other_evidence(tmp_path, fixture_data):
    path = tmp_path / "mixed.sqlite3"
    execute(parser().parse_args(["--db", str(path), "db-init"]))
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(fixture_data))
    execute(parser().parse_args(["--db", str(path), "replay", "--fixture", str(fixture)]))
    with pytest.raises(ValueError, match="dedicated"):
        execute(
            parser().parse_args(
                ["--db", str(path), "history", "acquire", "--from", DAY, "--through", DAY]
            )
        )
    summary = execute(parser().parse_args(["--db", str(path), "history", "summary"]))
    assert summary["status"] == "complete" and summary["markets"] == 0
    Store(path, min_free_bytes=0, readonly=True).close()


def test_execution_candles_are_a_separate_stage_features_never_read(store, config):
    calls = []
    reader = history_reader(store, config, handler_factory(calls))
    try:
        result = Acquisition(store, reader).run(
            start_date=date(2026, 9, 20), through_date=date(2026, 9, 20), post_cutoff_seconds=900
        )
    finally:
        reader.close()
    assert result["status"] == "complete" and result["candle_requests"]["ok"] == 8
    assert result["configuration"]["post_cutoff_seconds"] == 900
    post = [r for r in calls if r.url.path.endswith("/candlesticks") and "PHINYM-PHI" in r.url.path]
    windows = sorted(
        (
            int(parse_qs(r.url.query.decode())["start_ts"][0]),
            int(parse_qs(r.url.query.decode())["end_ts"][0]),
        )
        for r in post
    )
    cutoff = int(datetime(2026, 9, 20, 16, 10, tzinfo=timezone.utc).timestamp())
    assert windows == [(cutoff - 3600, cutoff), (cutoff + 60, cutoff + 900)]
    feature = interpret.candles(store.db, "KXMLBGAME-26SEP201310PHINYM-PHI")
    execution = interpret.execution_candles(store.db, "KXMLBGAME-26SEP201310PHINYM-PHI")
    limit = datetime.fromtimestamp(cutoff, timezone.utc)
    assert feature and all(c["end"] <= limit for c in feature)
    assert any(c["end"] > limit for c in execution)
    assert [c for c in execution if c["end"] <= limit] == feature


def with_start(pk, start):
    """SCHEDULE with one game's provider gameDate replaced."""
    games = [
        g | {"gameDate": start} if g["gamePk"] == pk else g for g in SCHEDULE["dates"][0]["games"]
    ]
    return {"dates": [{"date": DAY, "games": games}]}


@pytest.mark.parametrize("start", ["2026-09-20T17:10:00", "TBD", "2026-09-20", 1790010600])
def test_an_unparseable_official_start_is_one_unresolved_event(store, config, start):
    """Review F20: an offset-less or non-ISO gameDate on one candidate leaves that event
    unresolved (`unparseable_start`); the others identify and the run fetches candles."""
    calls = []
    result = acquire(store, config, calls, handler={"schedule": with_start(900001, start)})
    assert result["status"] == "complete", result
    assert result["identified_events"] == 1
    assert result["unresolved_by_reason"] == {"start_differs": 1, "unparseable_start": 1}
    candle_paths = sorted(r.url.path for r in calls if r.url.path.endswith("/candlesticks"))
    assert [p.rsplit("/", 2)[-2] for p in candle_paths] == [
        "KXMLBGAME-26SEP201610HOUATH-ATH",
        "KXMLBGAME-26SEP201610HOUATH-HOU",
    ]
    summary = interpret.summary(store.db)
    assert summary["unresolved_by_reason"]["unparseable_start"] == 1
    # A second candidate with the exact start cannot be proven unique beside it.
    games = interpret.schedule(store.db)
    twin = games[900001] | {"gamePk": 900009, "gameDate": "2026-09-20T17:10:00Z"}
    records = interpret.events(interpret.markets(store.db))["KXMLBGAME-26SEP201310PHINYM"]
    found = interpret.identify(records, games | {900009: twin})
    assert found == {"status": "unresolved", "reason": "unparseable_start"}


def test_the_request_bound_counts_logical_requests_and_reports_http_attempts(store, config, capsys):
    """Review N-20b: --max-requests bounds logical requests, and each may be retried up
    to max_attempts; HTTP attempts are reported separately as attempts_made."""
    calls = []
    unavailable = "KXMLBGAME-26SEP201310PHINYM-NYM"
    result = acquire(
        store, config, calls, max_requests=9, handler={"unavailable_ticker": unavailable}
    )
    assert result["status"] == "partial" and result["candle_requests"]["failed"] == 1
    assert result["requests_made"] == 9
    assert len(calls) == 9 - 1 + config.max_attempts == 11
    assert result["attempts_made"] == len(calls)
    assert store.db.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == len(calls)
    with pytest.raises(SystemExit):
        parser().parse_args(["history", "acquire", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "logical requests" in help_text and "attempts_made" in help_text
