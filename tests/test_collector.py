import json
from dataclasses import replace

import pytest
from conftest import Clock, response
from test_transport import reader_for

from vader_intelligence.collector import Collector
from vader_intelligence.transport import CollectionError


def handler_for(fixture_data, *, pages=None, no_games=False):
    entries = fixture_data["responses"]
    event = json.loads(next(x["body"] for x in entries if x["stage"] == "event"))
    schedule = json.loads(next(x["body"] for x in entries if x["stage"] == "schedule"))
    series = json.loads(entries[0]["body"])
    book = json.loads(entries[-1]["body"])
    if no_games:
        schedule = {"dates": []}
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        assert request.method == "GET"
        if path.endswith("/exchange/status"):
            return response(data={"exchange_active": True})
        if "/series/" in path:
            return response(data=series)
        if path.endswith(".pdf"):
            return response(body=b"%PDF-1.4\nsynthetic fixture\n%%EOF")
        if path.endswith("/markets"):
            if pages:
                return response(data=pages(request))
            return response(data={"markets": event["markets"], "cursor": ""})
        if "/events/" in path:
            return response(data=event)
        if path.endswith("/schedule"):
            return response(data=schedule)
        if path.endswith("/orderbook"):
            return response(data=book)
        raise AssertionError(f"unexpected request {request.url}")

    return handler, calls, event


def test_collection_end_to_end_with_pregame_proof(store, config, fixture_data):
    handler, calls, event = handler_for(fixture_data)
    result = Collector(store, reader_for(store, config, handler), config).run()
    assert result["status"] == "complete", result
    assert result["books"] == 2
    assert result["eligibility_reasons"] == {"eligible": 2}
    assert len([c for c in calls if c.url.path.endswith("/orderbook")]) == 2
    assert store.verify_integrity() == []
    rows = store.db.execute("SELECT data_json FROM observations WHERE kind='book'").fetchall()
    assert all(json.loads(r[0])["game_id"] == 824948 for r in rows)


def test_discovery_does_not_fetch_books(store, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)
    result = Collector(store, reader_for(store, config, handler), config).run(kind="discover")
    assert result["status"] == "complete"
    assert result["books"] == 0
    assert not any(c.url.path.endswith("/orderbook") for c in calls)


def test_empty_eligible_live_check_is_inconclusive(store, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data, no_games=True)
    result = Collector(store, reader_for(store, config, handler), config).run(kind="live-check")
    assert result["status"] == "inconclusive"
    assert result["books"] == 0
    assert result["eligibility_reasons"] == {"unmapped_game": 2}


def test_pagination_all_pages_follow_opaque_cursor(store, config, fixture_data, run):
    _, _, event = handler_for(fixture_data)

    def pages(request):
        second = request.url.params.get("cursor") == "opaque/+=?"
        return {
            "markets": [event["markets"][int(second)]],
            "cursor": "" if second else "opaque/+=?",
        }

    handler, calls, _ = handler_for(fixture_data, pages=pages)
    collector = Collector(store, reader_for(store, config, handler), config)
    assert len(collector.market_pages(run)) == 2
    assert calls[1].url.params["cursor"] == "opaque/+=?"


@pytest.mark.parametrize("bound", ["cursor", "pages", "markets"])
def test_pagination_bounds_are_explicit_failures(store, config, fixture_data, run, bound):
    _, _, event = handler_for(fixture_data)
    n = 0

    def pages(request):
        nonlocal n
        n += 1
        return {"markets": event["markets"], "cursor": "same" if bound == "cursor" else str(n)}

    handler, _, _ = handler_for(fixture_data, pages=pages)
    conf = replace(
        config,
        max_pages=2 if bound == "pages" else 100,
        max_markets=1 if bound == "markets" else 100,
    )
    with pytest.raises(CollectionError):
        Collector(store, reader_for(store, conf, handler), conf).market_pages(run)


def test_interrupted_pagination_reuses_committed_page(store, config, fixture_data, run):
    _, _, event = handler_for(fixture_data)
    call_count = 0

    def first(request):
        nonlocal call_count
        call_count += 1
        if request.url.params.get("cursor"):
            raise KeyboardInterrupt()
        return response(data={"markets": [event["markets"][0]], "cursor": "next"})

    collector = Collector(store, reader_for(store, config, first), config)
    with pytest.raises(KeyboardInterrupt):
        collector.market_pages(run)
    calls = []

    def second(request):
        calls.append(request)
        assert request.url.params.get("cursor") == "next"
        return response(data={"markets": [event["markets"][1]], "cursor": ""})

    recovered = Collector(store, reader_for(store, config, second), config).market_pages(run)
    assert len(recovered) == 2
    assert len(calls) == 1


def test_expired_cursor_restarts_scan_once(store, config, fixture_data, run):
    _, _, event = handler_for(fixture_data)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.params.get("cursor"):
            return response(400)
        return response(
            data={"markets": event["markets"], "cursor": "expired" if len(calls) == 1 else ""}
        )

    assert len(Collector(store, reader_for(store, config, handler), config).market_pages(run)) == 2
    assert len(calls) == 3


def test_book_received_after_cutoff_is_raw_only(store, config, fixture_data):
    handler, _, _ = handler_for(fixture_data)
    clock = Clock()
    clock.origin = clock.origin.replace(hour=19, minute=2, second=50)

    def delayed(request):
        result = handler(request)
        if request.url.path.endswith("/orderbook"):
            clock.value += 15
        return result

    result = Collector(store, reader_for(store, config, delayed, clock), config).run()
    assert result["status"] == "partial"
    assert result["books"] == 0
    assert (
        store.db.execute("SELECT COUNT(*) FROM fetches WHERE error='DeadlineExceeded'").fetchone()[
            0
        ]
        == 1
    )


def test_schedule_refreshed_before_stale_books(store, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)
    clock = Clock()
    conf = replace(config, schedule_max_age=10)

    def slow_event(request):
        if "/events/" in request.url.path:
            clock.value += 20
        return handler(request)

    result = Collector(store, reader_for(store, conf, slow_event, clock), conf).run()
    assert result["status"] == "complete", result
    assert len([c for c in calls if c.url.path.endswith("/schedule")]) == 2


def test_403_stops_entire_collection(store, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)

    def forbidden(request):
        if request.url.path.endswith("/orderbook"):
            return response(403)
        return handler(request)

    result = Collector(store, reader_for(store, config, forbidden), config).run()
    assert result["status"] == "failed"
    assert result["books"] == 0


def test_resume_with_only_historical_books_is_inconclusive(store, config, fixture_data):
    handler, _, _ = handler_for(fixture_data)
    books = 0

    def interrupt_second_book(request):
        nonlocal books
        if request.url.path.endswith("/orderbook"):
            books += 1
            if books == 2:
                raise KeyboardInterrupt()
        return handler(request)

    with pytest.raises(KeyboardInterrupt):
        Collector(store, reader_for(store, config, interrupt_second_book), config).run()
    run_id = store.db.execute("SELECT id FROM runs").fetchone()[0]
    handler, _, _ = handler_for(fixture_data, no_games=True)
    result = Collector(store, reader_for(store, config, handler), config).run(resume=run_id)
    assert result["books"] == 1
    assert result["books_this_pass"] == 0
    assert result["status"] == "inconclusive"
