"""Client: the fixed endpoint allowlist, environment base URLs, the transport guard,
read backoff on 429/5xx/transport errors, NO retry of writes, token buckets and strict
JSON. Every request goes to an httpx.MockTransport."""

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from vader_intelligence.trading.kalshi.auth import Signer
from vader_intelligence.trading.kalshi.client import (
    BASE_URLS,
    DuplicateClientOrderId,
    EndpointNotAllowed,
    KalshiClient,
    KalshiError,
    LiveTransportRefused,
    NotFound,
    ReadFailed,
    RequestRejected,
    ResponseFormatError,
    WriteOutcomeUnknown,
    match_endpoint,
)

from ._kalshi_double import KEY_ID, FakeClock

ORDER_ID = "0000000a-0000-4000-8000-00000000000a"


class Script:
    """MockTransport handler answering from a list of (status | exception, body)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, body = self.answers.pop(0) if self.answers else (200, {})
        if isinstance(status, Exception):
            raise status
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)


def make(script, **options):
    clock = FakeClock()
    client = KalshiClient(
        "demo",
        Signer(KEY_ID, ed25519.Ed25519PrivateKey.generate(), clock_ms=clock.ms),
        transport=httpx.MockTransport(script),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **options,
    )
    return client, clock


def test_allowlist_accepts_only_the_specified_endpoints():
    allowed = [
        ("POST", "/portfolio/events/orders", None, {"x": "1"}),
        ("DELETE", f"/portfolio/events/orders/{ORDER_ID}", {"market_ticker": "T"}, None),
        ("GET", "/portfolio/orders", {"ticker": "T", "min_ts": 1, "limit": 5}, None),
        ("GET", f"/portfolio/orders/{ORDER_ID}", None, None),
        ("GET", "/portfolio/fills", {"min_ts": 1, "order_id": ORDER_ID}, None),
        ("GET", "/portfolio/positions", {"limit": 5, "cursor": "c"}, None),
        ("GET", "/portfolio/settlements", {"min_ts": 1, "limit": 5, "cursor": "c"}, None),
        ("GET", "/portfolio/balance", None, None),
        ("GET", "/exchange/status", None, None),
    ]
    for method, path, params, body in allowed:
        match_endpoint(method, path, params, body)
    refused = [
        ("POST", "/portfolio/orders", None, {"x": "1"}),  # legacy create (deprecated)
        ("DELETE", f"/portfolio/orders/{ORDER_ID}", None, None),  # legacy cancel
        ("DELETE", "/portfolio/events/orders", None, None),  # cancel-all
        ("DELETE", "/portfolio/events/orders/batched", None, None),
        ("POST", "/portfolio/events/orders/batched", None, {"orders": []}),
        ("POST", f"/portfolio/events/orders/{ORDER_ID}/amend", None, {"x": "1"}),
        ("POST", f"/portfolio/events/orders/{ORDER_ID}/decrease", None, {"x": "1"}),
        ("GET", "/portfolio/orders/queue_positions", None, None),
        ("GET", "/portfolio/settlements", {"ticker": "T"}, None),  # unknown parameter
        ("GET", "/markets", None, None),
        ("GET", "/historical/orders", None, None),
        ("POST", "/portfolio/intra_exchange_instance_transfer", None, {"x": "1"}),
        ("GET", "/portfolio/orders/../balance", None, None),
        ("GET", "/portfolio/orders", {"subaccount": 1}, None),  # unknown query parameter
        ("GET", "/portfolio/balance", {"exchange_index": 3}, None),
        ("POST", "/portfolio/events/orders", None, None),  # create without a body
        ("GET", "/portfolio/balance", None, {"x": "1"}),  # read with a body
        ("PUT", "/portfolio/balance", None, None),
    ]
    script = Script()
    client, _ = make(script)
    for method, path, params, body in refused:
        with pytest.raises(EndpointNotAllowed):
            match_endpoint(method, path, params, body)
        with pytest.raises(EndpointNotAllowed):
            client.request(method, path, params=params, body=body)
    assert script.requests == []  # nothing was sent


def test_environment_base_urls_and_transport_guard():
    assert BASE_URLS == {
        "production": "https://external-api.kalshi.com/trade-api/v2",
        "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    }
    signer = Signer(KEY_ID, ed25519.Ed25519PrivateKey.generate())
    with pytest.raises(LiveTransportRefused):
        KalshiClient("demo", signer, transport=httpx.HTTPTransport())
    with pytest.raises(LiveTransportRefused):
        KalshiClient("demo", signer, transport=None)
    with pytest.raises(LiveTransportRefused):
        KalshiClient("demo", signer, transport=httpx.HTTPTransport(), network_key=object())
    with pytest.raises(KalshiError):
        KalshiClient("staging", signer, transport=httpx.MockTransport(Script()))
    script = Script((200, {"exchange_active": True}))
    production = KalshiClient("production", signer, transport=httpx.MockTransport(script))
    production.request("GET", "/exchange/status")
    assert (
        str(script.requests[0].url)
        == "https://external-api.kalshi.com/trade-api/v2/exchange/status"
    )
    assert "KALSHI-ACCESS-SIGNATURE" not in script.requests[0].headers  # public endpoint


def test_reads_back_off_exponentially_on_429_5xx_and_transport_errors():
    script = Script((429, {"error": "too many requests"}), (429, {}), (200, {"balance": 1}))
    client, clock = make(script)
    assert client.request("GET", "/portfolio/orders") == {"balance": 1}
    assert len(script.requests) == 3 and clock.sleeps == [0.5, 1.0]
    timeouts = Script(
        (httpx.ReadTimeout("synthetic"), None),
        (503, {}),
        (httpx.ConnectError("synthetic"), None),
        (500, {}),
    )
    client, clock = make(timeouts)
    with pytest.raises(ReadFailed, match="after 4 attempts"):
        client.request("GET", "/portfolio/fills")
    assert len(timeouts.requests) == 4 and clock.sleeps == [0.5, 1.0, 2.0]
    signatures = {r.headers["KALSHI-ACCESS-SIGNATURE"] for r in timeouts.requests}
    assert len(signatures) == 4  # every attempt is freshly signed
    rejected = Script((403, {"error": {"code": "forbidden"}}))
    client, clock = make(rejected)
    with pytest.raises(RequestRejected, match="forbidden"):
        client.request("GET", "/portfolio/positions")
    assert len(rejected.requests) == 1 and clock.sleeps == []
    missing = Script((404, {}))
    client, _ = make(missing)
    with pytest.raises(NotFound):
        client.request("GET", f"/portfolio/orders/{ORDER_ID}")
    redirect = Script((302, {}))
    client, _ = make(redirect)
    with pytest.raises(ReadFailed):
        client.request("GET", "/portfolio/balance")
    assert len(redirect.requests) == 1  # redirects are never followed


@pytest.mark.parametrize(
    "answer, error",
    [
        ((429, {"error": "too many requests"}), WriteOutcomeUnknown),
        ((500, {}), WriteOutcomeUnknown),
        ((503, {}), WriteOutcomeUnknown),
        ((302, {}), WriteOutcomeUnknown),
        ((httpx.ReadTimeout("synthetic"), None), WriteOutcomeUnknown),
        ((httpx.ConnectError("synthetic"), None), WriteOutcomeUnknown),
        ((httpx.RemoteProtocolError("synthetic"), None), WriteOutcomeUnknown),
        ((409, {"error": {"code": "order_already_exists"}}), DuplicateClientOrderId),
        ((400, {"error": {"code": "invalid_price"}}), RequestRejected),
        ((404, {}), NotFound),
    ],
)
def test_writes_are_never_retried(answer, error):
    for method, path, params, body in (
        ("POST", "/portfolio/events/orders", None, {"ticker": "SYN-X-Y"}),
        ("DELETE", f"/portfolio/events/orders/{ORDER_ID}", {"market_ticker": "SYN-X-Y"}, None),
    ):
        script = Script(answer, (201, {"order_id": ORDER_ID}))
        client, clock = make(script)
        with pytest.raises(error):
            client.request(method, path, params=params, body=body)
        assert len(script.requests) == 1 and clock.sleeps == []


def test_rejection_codes_are_sanitized():
    script = Script((400, {"error": {"code": "x" * 500, "message": "long"}}))
    client, _ = make(script)
    with pytest.raises(RequestRejected) as info:
        client.request("POST", "/portfolio/events/orders", body={"ticker": "SYN-X-Y"})
    assert info.value.code == "http_400" and info.value.status == 400


def test_token_buckets_throttle_writes_reads_and_balance():
    script = Script(*[(201, {"order_id": ORDER_ID})] * 6)
    client, clock = make(script)
    for _ in range(6):  # write bucket: 50 tokens, 10 per create, 50 tokens/s
        client.request("POST", "/portfolio/events/orders", body={"ticker": "SYN-X-Y"})
    assert clock.sleeps == [pytest.approx(0.2)]
    script = Script(*[(200, {})] * 3)
    client, clock = make(script)
    for _ in range(3):  # own balance bucket: one request per 2 s, no burst
        client.request("GET", "/portfolio/balance")
    assert clock.sleeps == [pytest.approx(2.0), pytest.approx(2.0)]
    script = Script(*[(200, {})] * 60)
    client, clock = make(script)
    for _ in range(60):  # GetOrder costs 2: 50 fit in the 100-token read burst
        client.request("GET", f"/portfolio/orders/{ORDER_ID}")
    assert len(clock.sleeps) == 10 and sum(clock.sleeps) == pytest.approx(0.2)


def test_json_bodies_are_strict_and_decimal():
    script = Script(
        (200, b'{"balance_dollars": "1.50", "x": 0.1}'),
        (200, b'{"x": NaN}'),
        (200, b"[1, 2]"),
        (200, b"not json"),
    )
    client, _ = make(script)
    data = client.request("GET", "/portfolio/balance")
    assert str(data["x"]) == "0.1" and type(data["x"]).__name__ == "Decimal"
    for _ in range(3):
        with pytest.raises(ResponseFormatError):
            client.request("GET", "/portfolio/positions")
    with pytest.raises(TypeError):
        client.request("POST", "/portfolio/events/orders", body={"price": 0.5})
