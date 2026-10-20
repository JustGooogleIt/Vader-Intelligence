"""`DryRunGateway`: builds the exact live requests and sends nothing.

It uses the live adapter's market rules, order mapping and request construction
(`client.request_parts`: allowlist, URL, query, canonical JSON body and, when the caller
supplies a `Signer` built from a THROWAWAY key, the three authentication headers). Each
would-be request is recorded in `requests`; no httpx client or transport exists inside
this class, so nothing can be sent.

`records()` (the export for reports) replaces each signature with its SHA-256: the
request body is not part of the signed text (facts §1), so a leaked signature could be
replayed with another body within the exchange's undocumented timestamp tolerance.
Never pass a real credential's signer to a dry run.

It is a `paper` gateway: the executor pairs it only with a paper ledger, so dry-run
results are recorded as simulated events and can never reach a live ledger. Submissions
return `submitted` and never fill; a cancel of a dry-run order returns `canceled`; with
`max_order_notional` / `max_daily_notional` the live gateway's local notional caps are
applied too (refusals `order_notional_cap` / `daily_notional_cap`);
`lookup` reports dry-run orders of this instance as open and anything else as absent
(nothing was ever placed); positions are empty and the balance is the caller's `cash`.
"""

import hashlib
from dataclasses import dataclass

from ...storage import utc_now
from ..gateway import CancelResult, OrderStatus, SubmitResult
from ..money import ZERO, digest, nonnegative, plain, positive
from .auth import HEADER_SIGNATURE
from .client import BASE_URLS, CREATE_ORDER_PATH, request_parts
from .gateway import MarketRule, prepare


@dataclass(frozen=True)
class DryRunRequest:
    """One request that would have been sent."""

    method: str
    url: str
    params: dict
    body: str | None
    headers: dict
    client_order_id: str
    at: object

    def to_dict(self):
        """JSON-safe record with the signature replaced by its SHA-256."""
        data = plain(self)
        signature = data["headers"].get(HEADER_SIGNATURE)
        if signature is not None:
            digest_text = hashlib.sha256(signature.encode()).hexdigest()
            data["headers"][HEADER_SIGNATURE] = "sha256:" + digest_text
        return data


class DryRunGateway:
    environment = "paper"

    def __init__(
        self,
        kalshi_environment,
        *,
        markets,
        cash,
        signer=None,
        now=utc_now,
        max_order_notional=None,
        max_daily_notional=None,
    ):
        if kalshi_environment not in BASE_URLS:
            raise ValueError(f"unknown Kalshi environment {kalshi_environment!r}")
        markets = tuple(markets)
        if not markets or not all(isinstance(m, MarketRule) for m in markets):
            raise ValueError("at least one MarketRule is required")
        self.kalshi_environment = kalshi_environment
        self.markets = markets
        self.cash = nonnegative(cash, "cash")
        self.signer = signer
        self.requests = []
        self.refusals = []
        self._now = now
        self._orders = {}  # client_order_id -> (exchange order id, ticker, status)
        self._halted = None
        self.max_order_notional = (
            None if max_order_notional is None else positive(max_order_notional, "order cap")
        )
        self.max_daily_notional = (
            None if max_daily_notional is None else positive(max_daily_notional, "daily cap")
        )
        self._daily = {}  # order date -> notional submitted

    def __repr__(self):
        return f"DryRunGateway(environment={self.kalshi_environment!r})"

    @property
    def halted(self):
        return self._halted is not None

    def halt(self, reason):
        if not isinstance(reason, str) or not reason:
            raise ValueError("a halt reason is required")
        self._halted = self._halted or reason

    def _record(self, method, path, client_order_id, *, params=None, body=None):
        _, url, query, content, headers = request_parts(
            self.kalshi_environment, self.signer, method, path, params=params, body=body
        )
        self.requests.append(
            DryRunRequest(
                method,
                url,
                query,
                content.decode() if content is not None else None,
                headers,
                client_order_id,
                self._now(),
            )
        )

    def records(self):
        return [r.to_dict() for r in self.requests]

    def submit(self, order):
        coid = order.client_order_id
        if self._halted is not None:
            self.refusals.append((coid, "halted"))
            return SubmitResult("rejected", reason="local:halted")
        request, refusal = prepare(order, self.markets)
        notional = order.quantity * order.limit_price
        day = order.created_at.date()
        if refusal is None and self.max_order_notional is not None:
            if notional > self.max_order_notional:
                refusal = "order_notional_cap"
        if refusal is None and self.max_daily_notional is not None:
            if self._daily.get(day, ZERO) + notional > self.max_daily_notional:
                refusal = "daily_notional_cap"
        if refusal is not None:
            self.refusals.append((coid, refusal))
            return SubmitResult("rejected", reason="local:" + refusal)
        self._daily[day] = self._daily.get(day, ZERO) + notional
        self._record("POST", CREATE_ORDER_PATH, coid, body=request.body())
        order_id = digest({"dry_run_order": coid})[:32]
        self._orders[coid] = (order_id, order.ticker, "open")
        return SubmitResult("submitted", exchange_order_id=order_id, reason="dry_run")

    def cancel(self, client_order_id):
        entry = self._orders.get(client_order_id)
        if entry is None or entry[2] != "open":
            return CancelResult("rejected", reason="not_found")
        order_id, ticker, _ = entry
        self._record(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            client_order_id,
            params={"market_ticker": ticker},
        )
        self._orders[client_order_id] = (order_id, ticker, "canceled")
        return CancelResult("canceled", reason="dry_run")

    def lookup(self, client_order_id):
        entry = self._orders.get(client_order_id)
        if entry is None:
            return None
        return OrderStatus(client_order_id, entry[2], ZERO, entry[0])

    def open_orders(self):
        return [
            OrderStatus(coid, "open", ZERO, entry[0])
            for coid, entry in sorted(self._orders.items())
            if entry[2] == "open"
        ]

    def fills_since(self, cursor):
        return [], cursor

    def positions(self):
        return {}

    def balance(self):
        return self.cash
