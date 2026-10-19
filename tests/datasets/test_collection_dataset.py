"""Collection-archive dataset (roadmap B2/H). Synthetic: the shipped fixture collected
with an injected transport, and a synthetic history archive for identity and labels."""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forecast"))
from _f34 import at, collect, fixture_data
from conftest import response
from history import test_acquire as synthetic

from vader_intelligence.datasets import collection
from vader_intelligence.history.acquire import Acquisition
from vader_intelligence.storage import Store

TICKER = "KXMLBGAME-26SEP271505HOUATH"


def history_archive(path, config):
    event = json.loads(
        next(r["body"] for r in fixture_data()["responses"] if r["stage"] == "event")
    )
    markets = []
    for market in event["markets"]:
        markets.append(
            market
            | {
                "status": "finalized",
                "result": "yes" if market["ticker"].endswith("-HOU") else "no",
                "settlement_value_dollars": "1.0000"
                if market["ticker"].endswith("-HOU")
                else "0.0000",
                "settlement_ts": "2026-09-27T22:30:00Z",
            }
        )
    schedule = json.loads(
        next(r["body"] for r in fixture_data()["responses"] if r["stage"] == "schedule")
    )
    game = schedule["dates"][0]["games"][0]
    game["status"] = {"abstractGameState": "Final", "detailedState": "Final", "startTimeTBD": False}
    game["teams"]["away"].update(score=5, isWinner=True)
    game["teams"]["home"].update(score=2, isWinner=False)

    def handler(request):
        path_ = request.url.path.removeprefix("/trade-api/v2")
        if path_ == "/historical/cutoff":
            return response(data={"market_settled_ts": "2026-08-19T00:00:00Z"})
        if path_ == "/historical/markets":
            return response(data={"markets": [], "cursor": ""})
        if path_ == "/markets":
            return response(data={"markets": markets, "cursor": ""})
        if request.url.host == "statsapi.mlb.com":
            return response(data=schedule)
        if path_.endswith("/candlesticks"):
            query = parse_qs(request.url.query.decode())
            return response(
                data={
                    "ticker": path_.split("/")[-2],
                    "candlesticks": [synthetic.candle_live(int(query["end_ts"][0]))],
                }
            )
        raise AssertionError(request.url)

    store = Store(path, min_free_bytes=1)
    reader = synthetic.history_reader(store, config, handler)
    try:
        Acquisition(store, reader).run(start_date=date(2026, 9, 27), through_date=date(2026, 9, 27))
    finally:
        reader.close()
    return store


def build(collection_db, history_db, *, lineage="synthetic", quote_source="listing"):
    return collection.build(
        collection_db,
        history_db,
        feature_set="fs-market-v1",
        quote_source=quote_source,
        start_date=date(2026, 9, 27),
        through_date=date(2026, 9, 27),
        name="t",
        lineage=lineage,
    )


@pytest.fixture
def archives(store, config, tmp_path):
    collect(store, config, origin=at(18, 3))  # injected transport: synthetic lineage
    history = history_archive(tmp_path / "history.sqlite3", config)
    try:
        yield store.db, history.db
    finally:
        history.close()


def test_rows_use_only_quotes_retrieved_by_the_cutoff_and_label_from_history(archives):
    collection_db, history_db = archives
    manifest, rows = build(collection_db, history_db)
    assert manifest["evidence_kind"] == "synthetic" and manifest["rows"] == 1
    (row,) = rows
    assert row["ticker"] == f"{TICKER}-HOU" and row["y"] == 1
    assert row["cutoff"] == "2026-09-27T18:05:00+00:00"
    assert datetime.fromisoformat(row["inputs"]["last_selected_quote_at"]) <= datetime(
        2026, 9, 27, 18, 5, tzinfo=timezone.utc
    )
    assert row["features"]["mid_c"] is not None
    books_manifest, books = build(collection_db, history_db, quote_source="book")
    assert books_manifest["source"]["quote_source"] == "book"
    assert books[0]["features"]["mid_c"] == pytest.approx(0.645)  # 0.64 bid / 1 - 0.35 ask


def test_live_lineage_never_admits_synthetic_collection(archives):
    collection_db, history_db = archives
    manifest, rows = build(collection_db, history_db, lineage="live")
    assert manifest["evidence_kind"] == "archived_collection" and rows == []
    assert manifest["exclusions"] == {"no_listing_before_cutoff": 1}


def test_quotes_after_the_cutoff_are_invisible(store, config, tmp_path):
    collect(store, config, origin=at(18, 6))  # every retrieval after C = 18:05
    history = history_archive(tmp_path / "history.sqlite3", config)
    try:
        manifest, rows = build(store.db, history.db)
    finally:
        history.close()
    assert rows == [] and manifest["exclusions"] == {"no_listing_before_cutoff": 1}
