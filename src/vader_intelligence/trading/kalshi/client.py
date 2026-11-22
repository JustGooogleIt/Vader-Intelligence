"""HTTP client for the allowlisted Kalshi Trade API v2 endpoints (live adapter only).

- Base URL by environment (facts §1 [V:ENV][V:DEMO]): `production` =
  https://external-api.kalshi.com/trade-api/v2, `demo` =
  https://external-api.demo.kalshi.co/trade-api/v2. No other host; redirects are not
  followed and proxy settings from the process environment are ignored.
- Fixed endpoint allowlist (docs/specs/live-adapter-v1.md): POST /portfolio/events/orders;
  DELETE /portfolio/events/orders/{order_id}; GET /portfolio/orders,
  /portfolio/orders/{order_id}, /portfolio/fills, /portfolio/positions,
  /portfolio/settlements, /portfolio/balance and /exchange/status. Any other method,
  path or query parameter is refused before a request is built.
- Token buckets (facts §5 [V:RL]): a read bucket (default 100 tokens/s, burst 100) and a
  write bucket (default 50 tokens/s, burst 50), half of the Basic tier (200 / 100), plus
  the balance endpoint's own bucket (default one request per 2 s; Basic allows 1/s with
  no burst). Costs: 10 tokens by default, 2 for GetOrder and cancel ([V:RL][V:OAS]).
- Reads (GET) retry with bounded exponential backoff on 429, 5xx and transport errors
  (RL: "Apply exponential backoff on 429"; 429 carries no Retry-After). Writes (POST,
  DELETE) are sent exactly once and never retried: a transport error, timeout, 3xx, 429
  or 5xx raises `WriteOutcomeUnknown`; 409 raises `DuplicateClientOrderId`; 404 raises
  `NotFound`; any other 4xx raises `RequestRejected` (definitive).
- The transport must be an `httpx.MockTransport` unless the caller passes the
  activation key, which only `activation.activate_live` uses.

Assumptions (UNVERIFIED or undocumented; also listed in docs/live-canary-runbook.md):

- a 429 or 5xx answer to a write may or may not have been processed (facts §10 [gap]):
  treated as unknown and resolved by reconciliation;
- 400/401/403 answers to a create mean the order was not placed ([INF]);
- the balance endpoint also draws read tokens (facts §5 lists only its own bucket);
- order ids are hexadecimal UUID-like strings: the allowlisted path pattern for
  `{order_id}` is `[0-9a-fA-F-]{8,64}` (ids that do not match are refused, fail closed).
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

from .auth import API_PREFIX

BASE_URLS = {
    "production": "https://external-api.kalshi.com" + API_PREFIX,
    "demo": "https://external-api.demo.kalshi.co" + API_PREFIX,
}
ORDER_ID = re.compile(r"[0-9a-fA-F-]{8,64}\Z")
CREATE_ORDER_PATH = "/portfolio/events/orders"
MAX_BODY_BYTES = 8 * 1024 * 1024
USER_AGENT = "Vader-Intelligence/0.1 live-adapter"
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")

# Only `activation.activate_live` passes this; any other transport than MockTransport is
# refused without it.
_NETWORK_TRANSPORT_KEY = object()

log = logging.getLogger(__name__)


class KalshiError(Exception):
    """Base class for adapter failures. Messages never contain headers or secrets."""


class EndpointNotAllowed(KalshiError):
    """The request is not on the fixed allowlist; nothing was built or sent."""


class LiveTransportRefused(KalshiError):
    """A network transport without activation."""


class WriteOutcomeUnknown(KalshiError):
    """A write was (or may have been) sent and its outcome is unknown."""


class DuplicateClientOrderId(KalshiError):
    """409 on create: an order with this client_order_id already exists (facts §2.3)."""


class NotFound(KalshiError):
    """404 from the exchange."""


class RequestRejected(KalshiError):
    """A definitive 4xx refusal."""

    def __init__(self, endpoint, status, code):
        self.endpoint, self.status, self.code = endpoint, status, code
        super().__init__(f"{endpoint}: HTTP {status} ({code})")


class ReadFailed(KalshiError):
    """A read did not succeed within the retry budget."""


class ResponseFormatError(KalshiError):
    """A response body that is not the documented JSON object."""


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    pattern: re.Pattern
    write: bool
    auth: bool
    cost: int
    params: frozenset
    balance: bool = False


def _endpoint(name, method, pattern, *, write=False, auth=True, cost=10, params=(), **kw):
    return Endpoint(
        name, method, re.compile(pattern + r"\Z"), write, auth, cost, frozenset(params), **kw
    )


_ID = r"[0-9a-fA-F-]{8,64}"
ENDPOINTS = (
    _endpoint("create_order", "POST", re.escape(CREATE_ORDER_PATH), write=True),
    _endpoint(
        "cancel_order",
        "DELETE",
        r"/portfolio/events/orders/" + _ID,
        write=True,
        cost=2,
        params=("market_ticker",),
    ),
    _endpoint(
        "list_orders",
        "GET",
        r"/portfolio/orders",
        params=("ticker", "min_ts", "status", "limit", "cursor"),
    ),
    _endpoint("get_order", "GET", r"/portfolio/orders/" + _ID, cost=2),
    _endpoint(
        "list_fills", "GET", r"/portfolio/fills", params=("min_ts", "order_id", "limit", "cursor")
    ),
    _endpoint("positions", "GET", r"/portfolio/positions", params=("limit", "cursor")),
    _endpoint(
        "list_settlements", "GET", r"/portfolio/settlements", params=("min_ts", "limit", "cursor")
    ),
    _endpoint("balance", "GET", r"/portfolio/balance", balance=True),
    _endpoint("exchange_status", "GET", r"/exchange/status", auth=False),
)


def match_endpoint(method, path, params=None, body=None):
    """The allowlisted endpoint for this request, or `EndpointNotAllowed`."""
    for endpoint in ENDPOINTS:
        if endpoint.method == method and endpoint.pattern.fullmatch(path or ""):
            extra = set(params or {}) - endpoint.params
            if extra:
                raise EndpointNotAllowed(f"{endpoint.name}: query parameters {sorted(extra)}")
            if (body is not None) != (endpoint.name == "create_order"):
                raise EndpointNotAllowed(f"{endpoint.name}: unexpected request body")
            return endpoint
    raise EndpointNotAllowed(f"{method} {path!r} is not an allowlisted Kalshi endpoint")


def encode_body(body):
    """Canonical JSON for a request body; floats are refused (money is Decimal text)."""

    def refuse(value):
        raise TypeError(f"unsupported body value {type(value).__name__}")

    def check(value):
        if isinstance(value, float):
            raise TypeError("floats are not permitted in request bodies")
        if isinstance(value, dict):
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(body)
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=refuse).encode()


# Write responses that definitively mean "not executed" (facts: documented 400/401/403;
# 422 for validation); every other non-2xx write answer leaves the outcome unknown.
DEFINITIVE_WRITE_REJECTIONS = frozenset((400, 401, 403, 422))


def request_parts(environment, signer, method, path, *, params=None, body=None):
    """Everything needed to send one allowlisted request: (endpoint, url, params,
    content, headers). Shared by the live client and the dry-run gateway."""
    if environment not in BASE_URLS:
        raise KalshiError(f"unknown Kalshi environment {environment!r}")
    endpoint = match_endpoint(method, path, params, body)
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    content = None
    if body is not None:
        content = encode_body(body)
        headers["Content-Type"] = "application/json"
    if endpoint.auth and signer is not None:
        creates = endpoint.write and method == "POST"  # only order creation opens exposure
        headers.update(signer.headers(method, API_PREFIX + path, check_clock=creates))
    query = {k: str(v) for k, v in sorted((params or {}).items())}
    return endpoint, BASE_URLS[environment] + path, query, content, headers


def error_code(response):
    """A short, sanitized error code from an error body (never the full message)."""
    try:
        data = json.loads(response.content)
    except (ValueError, RecursionError):
        data = None
    code = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            code = error.get("code")
        elif isinstance(data.get("code"), str):
            code = data["code"]
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code
    return f"http_{response.status_code}"


def parse_json(response):
    """The response body as a JSON object; floats become Decimal, NaN is refused."""
    content = response.content
    if len(content) > MAX_BODY_BYTES:
        raise ResponseFormatError("response body too large")
    if not content:
        return {}

    def refuse(name):
        raise ValueError(f"non-finite number {name}")

    failed = False
    try:
        data = json.loads(content, parse_float=Decimal, parse_constant=refuse)
    except (ValueError, RecursionError):
        failed = True
    if failed:
        raise ResponseFormatError("response body is not valid JSON")
    if not isinstance(data, dict):
        raise ResponseFormatError("response body is not a JSON object")
    return data


class TokenBucket:
    """Blocking token bucket on an injectable monotonic clock (seconds, not money)."""

    def __init__(self, rate, capacity, *, monotonic=time.monotonic, sleep=time.sleep):
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate, self.capacity = rate, capacity
        self._monotonic, self._sleep = monotonic, sleep
        self._tokens = capacity
        self._stamp = monotonic()

    def _refill(self):
        now = self._monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now

    def acquire(self, cost):
        if cost > self.capacity:
            raise ValueError("request cost exceeds the bucket capacity")
        self._refill()
        if self._tokens < cost:
            self._sleep((cost - self._tokens) / self.rate)
            self._refill()
        self._tokens = max(0, self._tokens - cost)


@dataclass(frozen=True)
class RetryPolicy:
    """Read retries only: `attempts` tries in total, delay base * 2**i capped."""

    attempts: int = 4
    base_seconds: float = 0.5
    cap_seconds: float = 4.0

    def delay(self, attempt):
        return min(self.cap_seconds, self.base_seconds * (2**attempt))


class KalshiClient:
    """Sends allowlisted requests. One instance per environment and key."""

    def __init__(
        self,
        environment,
        signer,
        *,
        transport,
        timeout=DEFAULT_TIMEOUT,
        retry=RetryPolicy(),
        read_rate=100,
        read_capacity=100,
        write_rate=50,
        write_capacity=50,
        balance_interval_seconds=2.0,
        monotonic=time.monotonic,
        sleep=time.sleep,
        network_key=None,
    ):
        if environment not in BASE_URLS:
            raise KalshiError(f"unknown Kalshi environment {environment!r}")
        if not isinstance(transport, httpx.BaseTransport):
            raise LiveTransportRefused("an explicit httpx transport is required")
        if not isinstance(transport, httpx.MockTransport) and network_key is not (
            _NETWORK_TRANSPORT_KEY
        ):
            raise LiveTransportRefused(
                "network transports are created only by activation.activate_live"
            )
        if signer is None:
            raise KalshiError("the live client requires a signer")
        self.environment = environment
        self.signer = signer
        self.retry = retry
        self._sleep = sleep
        self.read_bucket = TokenBucket(read_rate, read_capacity, monotonic=monotonic, sleep=sleep)
        self.write_bucket = TokenBucket(
            write_rate, write_capacity, monotonic=monotonic, sleep=sleep
        )
        self.balance_bucket = TokenBucket(
            1 / balance_interval_seconds, 1, monotonic=monotonic, sleep=sleep
        )
        self._http = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def __repr__(self):
        return f"KalshiClient(environment={self.environment!r})"

    def close(self):
        self._http.close()

    def _acquire(self, endpoint):
        if endpoint.write:
            self.write_bucket.acquire(endpoint.cost)
            return
        if endpoint.balance:
            self.balance_bucket.acquire(1)
        self.read_bucket.acquire(endpoint.cost)

    def _build(self, method, path, params, body):
        endpoint, url, query, content, headers = request_parts(
            self.environment, self.signer, method, path, params=params, body=body
        )
        request = self._http.build_request(
            method, url, params=query or None, content=content, headers=headers
        )
        if request.url.path != API_PREFIX + path or request.url.host != httpx.URL(url).host:
            raise EndpointNotAllowed("built request does not match the signed path")
        return endpoint, request

    def _observe(self, response, endpoint):
        # Runs after a response arrived: it must never raise (a write's outcome would
        # otherwise be misreported). A failed observation simply records nothing. Only
        # authenticated, uncached responses are observed: a public endpoint's Date may
        # come from a cache (an `Age` header, or `cache-control: public`, facts L1).
        if not endpoint.auth or "age" in response.headers:
            return
        try:
            self.signer.observe_server_date(response.headers.get("date"), self.signer.clock_ms())
        except Exception:
            log.warning("could not record the exchange Date header")

    def request(self, method, path, *, params=None, body=None):
        """Send one allowlisted request and return its JSON object."""
        endpoint = match_endpoint(method, path, params, body)
        if endpoint.write:
            return self._write(endpoint, method, path, params, body)
        return self._read(endpoint, method, path, params)

    def _write(self, endpoint, method, path, params, body):
        self._acquire(endpoint)
        _, request = self._build(method, path, params, body)
        try:
            response = self._http.send(request)
        except httpx.RequestError as exc:
            log.warning("%s: %s; outcome unknown, not retried", endpoint.name, type(exc).__name__)
            raise WriteOutcomeUnknown(f"{endpoint.name}: {type(exc).__name__}") from None
        self._observe(response, endpoint)
        status = response.status_code
        if 200 <= status < 300:
            return parse_json(response)
        if status == 409:
            raise DuplicateClientOrderId(f"{endpoint.name}: HTTP 409")
        if status == 404:
            raise NotFound(f"{endpoint.name}: HTTP 404")
        if status not in DEFINITIVE_WRITE_REJECTIONS:
            # Only documented rejections mean "not placed"; anything else (408, 425, 429,
            # 5xx, an unexpected 4xx or 3xx) leaves the outcome unknown: reconcile.
            log.warning("%s: HTTP %d; outcome unknown, not retried", endpoint.name, status)
            raise WriteOutcomeUnknown(f"{endpoint.name}: HTTP {status}")
        raise RequestRejected(endpoint.name, status, error_code(response))

    def _read(self, endpoint, method, path, params):
        failure = "no attempt"
        for attempt in range(self.retry.attempts):
            self._acquire(endpoint)
            _, request = self._build(method, path, params, None)
            try:
                response = self._http.send(request)
            except httpx.RequestError as exc:
                failure = type(exc).__name__
            else:
                self._observe(response, endpoint)
                status = response.status_code
                if 200 <= status < 300:
                    return parse_json(response)
                if status == 404:
                    raise NotFound(f"{endpoint.name}: HTTP 404")
                if status != 429 and status < 500:
                    if status < 400:
                        raise ReadFailed(f"{endpoint.name}: HTTP {status}")
                    raise RequestRejected(endpoint.name, status, error_code(response))
                failure = f"HTTP {status}"
            if attempt + 1 < self.retry.attempts:
                delay = self.retry.delay(attempt)
                log.info("%s: %s; retrying read in %.2f s", endpoint.name, failure, delay)
                self._sleep(delay)
        raise ReadFailed(f"{endpoint.name}: {failure} after {self.retry.attempts} attempts")
