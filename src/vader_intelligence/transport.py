import hashlib
import random
import re
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .normalize import normalizer
from .storage import timestamp, utc_now

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"


class CollectionError(RuntimeError):
    pass


class HTTPFailure(CollectionError):
    def __init__(self, status, fetch_id):
        self.status = status
        self.fetch_id = fetch_id
        super().__init__(f"HTTP {status}; retrieval {fetch_id}")


class DeadlineExceeded(CollectionError):
    pass


@contextmanager
def deadline_guard(seconds):
    # The CLI runs on Unix's main thread. Unlike read timeouts, this also bounds a
    # peer that continually trickles bytes without ever completing a response.
    if threading.current_thread() is not threading.main_thread():
        raise CollectionError("HTTP collection must run on the main thread")
    if signal.getitimer(signal.ITIMER_REAL)[0] > 0:
        raise CollectionError("an existing process alarm conflicts with the HTTP deadline")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise DeadlineExceeded("total request deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.000001))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def validate_source(url, params):
    part = urlsplit(url)
    if part.scheme != "https" or part.query or part.fragment or part.username or part.port:
        raise ValueError("only canonical HTTPS source URLs are allowed")
    if part.hostname == "external-api.kalshi.com":
        path = part.path.removeprefix("/trade-api/v2")
        if not part.path.startswith("/trade-api/v2/") or not re.fullmatch(
            r"/(?:exchange/status|series/KXMLBGAME|markets|events/[A-Z0-9_-]+|"
            r"markets/[A-Z0-9_-]+/orderbook)",
            path,
        ):
            raise ValueError("Kalshi endpoint is not allowed")
        allowed = {"series_ticker", "status", "limit", "cursor", "depth"}
        if set(params) - allowed:
            raise ValueError("unsupported Kalshi query parameter")
        return "kalshi"
    if part.hostname == "statsapi.mlb.com" and part.path == "/api/v1/schedule":
        if set(params) - {"sportId", "startDate", "endDate"} or str(params.get("sportId")) != "1":
            raise ValueError("only MLB schedule queries are allowed")
        return "mlb"
    if (
        part.hostname == "assets.kalshi.com"
        and re.fullmatch(
            r"/(?:contract_terms|regulatory/product-certifications)/[A-Za-z0-9_-]+\.pdf", part.path
        )
        and not params
    ):
        return "kalshi_document"
    raise ValueError("source host/path is not allowed")


@dataclass
class Response:
    fetch_id: str
    body: bytes
    retrieved_at: str


class Reader:
    def __init__(
        self,
        store,
        config,
        *,
        client=None,
        now=utc_now,
        monotonic=time.monotonic,
        sleep=time.sleep,
        jitter=random.uniform,
    ):
        self.store, self.config = store, config
        self.client = client or httpx.Client(
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": "Vader-Intelligence/0.1 read-only research",
                "Accept-Encoding": "identity",
            },
        )
        self.now, self.monotonic, self.sleep, self.jitter = now, monotonic, sleep, jitter
        self.next_request = 0.0

    def close(self):
        self.client.close()

    def get(
        self, run_id, stage, key, url, params=None, *, apply=None, budget=None, before_attempt=None
    ):
        params = params or {}
        source = validate_source(url, params)
        request = self.store.request(run_id, stage, key, url, params)
        if request["done"]:
            row = self.store.saved(request["id"])
            if hashlib.sha256(row["body"]).hexdigest() != row["body_sha256"]:
                raise CollectionError("saved body hash mismatch")
            return Response(row["id"], row["body"], row["retrieved_at"])
        total = (
            min(self.config.request_budget, budget)
            if budget is not None
            else self.config.request_budget
        )
        end = self.monotonic() + total
        apply = apply or normalizer(stage, key)
        for attempt in range(self.config.max_attempts):
            wait = max(0.0, self.next_request - self.monotonic())
            if wait >= end - self.monotonic():
                raise DeadlineExceeded("request budget exhausted waiting for local rate limit")
            self.sleep(wait)
            remaining = end - self.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded("request budget exhausted")
            if before_attempt:
                before_attempt()
            started = self.monotonic()
            fetch_id = self.store.begin_fetch(request["id"], timestamp(self.now()))
            body = bytearray()
            status = None
            headers = {"source": source}
            error = None
            truncated = False
            retry = False
            retry_after = 0.0
            limit = (
                self.config.max_document_bytes
                if stage == "document"
                else self.config.max_json_bytes
            )
            try:
                # Persistence, rate waits, retries or host sleep may invalidate the
                # eligibility checked by the caller. Recheck just before every send.
                if before_attempt:
                    before_attempt()
                remaining = end - self.monotonic()
                if remaining <= 0:
                    raise DeadlineExceeded("request budget exhausted before send")
                self.next_request = self.monotonic() + 1 / self.config.requests_per_second
                with deadline_guard(remaining):
                    with self.client.stream(
                        "GET",
                        url,
                        params=params,
                        timeout=httpx.Timeout(
                            min(self.config.read_timeout, remaining),
                            connect=min(self.config.connect_timeout, remaining),
                        ),
                    ) as response:
                        status = response.status_code
                        headers.update(
                            {
                                k: response.headers[k]
                                for k in (
                                    "date",
                                    "content-type",
                                    "etag",
                                    "last-modified",
                                    "retry-after",
                                    "x-request-id",
                                    "content-encoding",
                                )
                                if k in response.headers
                            }
                        )
                        for chunk in response.iter_raw(chunk_size=65536):
                            if self.monotonic() >= end:
                                raise DeadlineExceeded("total request deadline exceeded")
                            if len(body) + len(chunk) > limit:
                                body.extend(chunk[: max(0, limit - len(body))])
                                truncated = True
                                raise CollectionError("response exceeds byte limit")
                            body.extend(chunk)
                        if status != 200:
                            error = f"HTTP {status}"
                        elif response.headers.get("content-encoding", "identity") != "identity":
                            error = "unsupported_content_encoding"
                        retry = status == 429 or status >= 500
                        if retry and "retry-after" in response.headers:
                            raw = response.headers["retry-after"]
                            try:
                                retry_after = max(0.0, float(raw))
                            except ValueError:
                                try:
                                    retry_after = max(
                                        0.0,
                                        (parsedate_to_datetime(raw) - self.now()).total_seconds(),
                                    )
                                except (ValueError, TypeError, OverflowError):
                                    retry_after = 0.0
            except (httpx.TransportError, DeadlineExceeded, CollectionError) as exc:
                error = type(exc).__name__
                truncated = status is not None or truncated
                retry = isinstance(exc, httpx.TransportError)
            except BaseException:
                self.store.complete_fetch(
                    fetch_id,
                    body=bytes(body) if status is not None else None,
                    status=status,
                    headers=headers,
                    retrieved_at=timestamp(self.now()),
                    elapsed=self.monotonic() - started,
                    error="interrupted",
                    truncated=status is not None,
                )
                raise
            received = timestamp(self.now())
            parse_error = self.store.complete_fetch(
                fetch_id,
                body=bytes(body) if status is not None else None,
                status=status,
                headers=headers,
                retrieved_at=received,
                elapsed=self.monotonic() - started,
                error=error,
                truncated=truncated,
                normalizer=apply,
            )
            if not error and not parse_error:
                return Response(fetch_id, bytes(body), received)
            if status in (401, 403) or (status is not None and status != 200 and not retry):
                raise HTTPFailure(status, fetch_id)
            if not retry or parse_error or attempt + 1 >= self.config.max_attempts:
                raise CollectionError(f"{parse_error or error}; retrieval {fetch_id}")
            delay = max(retry_after, self.jitter(0, min(8, 0.5 * 2**attempt)))
            if delay >= end - self.monotonic():
                raise DeadlineExceeded(f"retry exceeds budget; retrieval {fetch_id}")
            self.sleep(delay)
        raise CollectionError("request attempts exhausted")
