import signal
import time
from dataclasses import replace

import httpx
import pytest
from conftest import Clock, response

from vader_intelligence.transport import (
    KALSHI,
    CollectionError,
    DeadlineExceeded,
    HTTPFailure,
    Reader,
    validate_source,
)


def reader_for(store, config, handler, clock=None):
    clock = clock or Clock()
    return Reader(
        store,
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=clock.now,
        monotonic=clock.mono,
        sleep=clock.sleep,
        jitter=lambda a, b: b,
    )


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_then_success_archives_both_attempts(store, config, run, status):
    calls = []

    def handler(request):
        calls.append(request)
        return response(
            status if len(calls) == 1 else 200,
            {"exchange_active": True},
            headers={"Retry-After": "1"},
        )

    clock = Clock()
    reader = reader_for(store, config, handler, clock)
    result = reader.get(run, "status", "k", KALSHI + "/exchange/status")
    assert len(calls) == 2
    assert clock.value >= 1
    assert [row[0] for row in store.db.execute("SELECT status FROM fetches ORDER BY seq")] == [
        status,
        200,
    ]
    assert result.fetch_id


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_terminal_http_never_retries_or_redirects(store, config, run, status):
    calls = []

    def handler(request):
        calls.append(request)
        return response(status, headers={"Location": "https://evil.invalid"})

    with pytest.raises(HTTPFailure) as exc:
        reader_for(store, config, handler).get(run, "status", "k", KALSHI + "/exchange/status")
    assert exc.value.status == status
    assert len(calls) == 1
    assert store.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1


def test_network_timeouts_are_bounded_and_archived(store, config, run):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("test timeout")

    with pytest.raises(CollectionError):
        reader_for(store, config, handler).get(run, "status", "k", KALSHI + "/exchange/status")
    assert len(calls) == 3
    assert (
        store.db.execute("SELECT COUNT(*) FROM fetches WHERE error='ReadTimeout'").fetchone()[0]
        == 3
    )


def test_retry_after_cannot_exceed_total_budget(store, config, run):
    calls = []

    def handler(request):
        calls.append(request)
        return response(429, headers={"Retry-After": "999"})

    with pytest.raises(DeadlineExceeded):
        reader_for(store, config, handler).get(run, "status", "k", KALSHI + "/exchange/status")
    assert len(calls) == 1


def test_rate_limit_and_saved_request_reuse(store, config, run):
    clock = Clock()
    starts = []

    def handler(request):
        starts.append(clock.value)
        return response(data={"exchange_active": True})

    reader = reader_for(store, config, handler, clock)
    first = reader.get(run, "status", "one", KALSHI + "/exchange/status")
    saved = reader.get(run, "status", "one", KALSHI + "/exchange/status")
    second = reader.get(run, "status", "two", KALSHI + "/exchange/status")
    assert first.fetch_id == saved.fetch_id != second.fetch_id
    assert starts == [0, 0.5]


def test_oversize_body_archived_as_truncated_and_not_parsed(store, config, run):
    reader = reader_for(
        store, replace(config, max_json_bytes=20), lambda r: response(body=b"x" * 100)
    )
    with pytest.raises(CollectionError):
        reader.get(run, "status", "k", KALSHI + "/exchange/status")
    row = store.db.execute(
        "SELECT f.truncated,b.byte_count FROM fetches f JOIN blobs b ON f.body_sha256=b.sha256"
    ).fetchone()
    assert tuple(row) == (1, 20)
    assert store.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0


def test_compressed_body_is_bounded_without_decompression(store, config, run):
    import gzip

    body = gzip.compress(b"X" * 100000)
    reader = reader_for(
        store, config, lambda r: response(body=body, headers={"Content-Encoding": "gzip"})
    )
    with pytest.raises(CollectionError, match="unsupported_content_encoding"):
        reader.get(run, "status", "k", KALSHI + "/exchange/status")
    assert store.db.execute("SELECT body FROM blobs").fetchone()[0] == body


def test_malformed_json_is_terminal_and_preserved(store, config, run):
    reader = reader_for(store, config, lambda r: response(body=b"not json"))
    with pytest.raises(CollectionError):
        reader.get(run, "status", "k", KALSHI + "/exchange/status")
    assert store.db.execute("SELECT state FROM fetches").fetchone()[0] == "parse_error"


def test_total_deadline_interrupts_trickling_stream(store, config, run):
    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{"
            time.sleep(0.5)
            yield b"}"

    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow()))
    )
    reader = Reader(store, replace(config, request_budget=0.05), client=client)
    old = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(CollectionError):
        reader.get(run, "status", "k", KALSHI + "/exchange/status")
    assert time.monotonic() - started < 0.4
    assert signal.getsignal(signal.SIGALRM) == old
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert store.db.execute("SELECT error FROM fetches").fetchone()[0] == "DeadlineExceeded"


@pytest.mark.parametrize(
    "url,params",
    [
        ("http://external-api.kalshi.com/trade-api/v2/markets", {}),
        ("https://external-api.kalshi.com.evil.invalid/trade-api/v2/markets", {}),
        (KALSHI + "/portfolio/orders", {}),
        (KALSHI + "/markets?token=secret", {}),
        ("https://user@external-api.kalshi.com/trade-api/v2/markets", {}),
        ("https://assets.kalshi.com/contract_terms/../../file.pdf", {}),
        ("https://statsapi.mlb.com/api/v1/schedule", {"sportId": 2}),
    ],
)
def test_unapproved_network_targets_rejected_before_request(url, params):
    with pytest.raises(ValueError):
        validate_source(url, params)
