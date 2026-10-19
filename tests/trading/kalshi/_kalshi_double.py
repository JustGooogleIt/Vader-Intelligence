"""SYNTHETIC Kalshi Trade API v2 exchange double for `httpx.MockTransport`, plus helpers.

The double implements only the documented V2 endpoints the adapter allowlists
(docs/specs/kalshi-api-facts-2026-10-18.md §2, §5, §9): create/cancel orders, list/get
orders, fills, positions, balance and exchange status. It verifies the signature of
every authenticated request with the public half of a key generated inside the test,
keeps its own independent cash/position accounting, and can inject timeouts (before or
after processing), 429/5xx answers, REST visibility lag and cancel/fill races.
Nothing here is market evidence; tickers and prices are invented.
"""

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import format_datetime

import httpx
from _trading_synthetic import T0, D, at
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from vader_intelligence.trading.executor import Executor
from vader_intelligence.trading.fees import KALSHI_2026_02_GENERAL as FEES
from vader_intelligence.trading.kalshi.auth import Signer
from vader_intelligence.trading.kalshi.client import KalshiClient
from vader_intelligence.trading.kalshi.gateway import KalshiGateway, MarketRule
from vader_intelligence.trading.kalshi.mapping import price_ranges
from vader_intelligence.trading.ledger import Ledger
from vader_intelligence.trading.portfolio import CashEvent
from vader_intelligence.trading.risk import RiskEngine, simulation_defaults

KEY_ID = "0b6e2d5c-1f3a-4c8e-9d7b-5a4f3e2d1c0b"
PREFIX = "/trade-api/v2"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
LINEAR_CENT = ({"start": "0.0000", "end": "1.0000", "step": "0.0100"},)
SYN_RULE = MarketRule("SYN", price_ranges(LINEAR_CENT), exchange_index=3)
_CREATE_REQUIRED = {
    "ticker",
    "side",
    "count",
    "price",
    "time_in_force",
    "self_trade_prevention_type",
}
_CREATE_ALLOWED = _CREATE_REQUIRED | {
    "client_order_id",
    "post_only",
    "reduce_only",
    "exchange_index",
    "expiration_time",
    "cancel_order_on_pause",
    "subaccount",
    "order_group_id",
}


def fp(value, places):
    return format(Decimal(value).quantize(Decimal(1).scaleb(-places)), "f")


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeClock:
    """One synthetic clock for the signer, the client's buckets, the gateway and the
    double. `sleep` advances it (and records the delay)."""

    def __init__(self, start=T0):
        self.value = start
        self.sleeps = []

    def now(self):
        return self.value

    def ms(self):
        return (self.value - EPOCH) // timedelta(milliseconds=1)

    def monotonic(self):
        return (self.value - T0).total_seconds()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class KalshiDouble:
    def __init__(self, clock, public_key, *, key_id=KEY_ID, cash="1000", page_size=2):
        self.clock = clock
        self.public_key = public_key
        self.key_id = key_id
        self.cash = D(cash)
        self.page_size = page_size
        self.exchange_index = 3
        self.orders = {}  # order_id -> internal dict
        self.fill_log = []  # internal dicts, oldest first
        self.positions = {}  # ticker -> signed YES position
        self.calls = []  # (method, path, params, body)
        self.auth_failures = 0
        self.unknown = []
        self.failures = []  # [method, compiled path pattern, mode, remaining]
        self.lag = False  # new orders hidden from REST reads until published
        self.hidden_orders = set()
        self.hidden_fills = set()
        self.duplicate_pages = False
        self.reject_create = None  # error code for a 400 on the next create
        self.on_cancel = None  # hook(order) run before a cancel takes effect
        self.immediate = None  # hook(order) -> [(count, yes_price, fee)] on create
        self.date_offset = 0
        self.zero_position_rows = ()
        self._orders_n = 0
        self._fills_n = 0

    # -- plumbing ----------------------------------------------------------------

    def transport(self):
        return httpx.MockTransport(self.handle)

    def inject(self, method, pattern, mode, times=1):
        self.failures.append([method, re.compile(pattern), mode, times])

    def _take_failure(self, method, path):
        for entry in self.failures:
            if entry[0] == method and entry[1].fullmatch(path) and entry[3] > 0:
                entry[3] -= 1
                return entry[2]
        return None

    def calls_to(self, method, pattern=".*"):
        regex = re.compile(PREFIX + pattern)
        return [c for c in self.calls if c[0] == method and regex.fullmatch(c[1])]

    def _reply(self, status, data=None):
        headers = {
            "Date": format_datetime(
                self.clock.now() + timedelta(seconds=self.date_offset), usegmt=True
            ),
            "Content-Type": "application/json",
        }
        content = json.dumps(data).encode() if data is not None else b""
        return httpx.Response(status, headers=headers, content=content)

    def _error(self, status, code):
        return self._reply(status, {"error": {"code": code, "message": "synthetic " + code}})

    def handle(self, request):
        method, path = request.method, request.url.path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, params, body))
        mode = self._take_failure(method, path)
        if mode == "timeout_before":
            raise httpx.ConnectTimeout("synthetic timeout before processing", request=request)
        if mode == 429:
            return self._reply(429, {"error": "too many requests"})
        if isinstance(mode, int):
            return self._error(mode, "synthetic_failure")
        if not path.startswith(PREFIX + "/"):
            return self._error(404, "not_found")
        sub = path[len(PREFIX) :]
        if sub != "/exchange/status" and not self._authentic(request):
            self.auth_failures += 1
            return self._error(401, "authentication_error")
        response = self._route(method, sub, params, body)
        if mode == "timeout_after":
            raise httpx.ReadTimeout("synthetic: processed, response lost", request=request)
        return response

    def _authentic(self, request):
        headers = request.headers
        key = headers.get("KALSHI-ACCESS-KEY")
        ts = headers.get("KALSHI-ACCESS-TIMESTAMP")
        sig = headers.get("KALSHI-ACCESS-SIGNATURE")
        if key != self.key_id or not ts or not re.fullmatch(r"\d{13}", ts) or not sig:
            return False
        message = (ts + request.method + request.url.path).encode()
        try:
            signature = base64.b64decode(sig, validate=True)
            if isinstance(self.public_key, rsa.RSAPublicKey):
                self.public_key.verify(
                    signature,
                    message,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                    hashes.SHA256(),
                )
            elif isinstance(self.public_key, ed25519.Ed25519PublicKey):
                self.public_key.verify(signature, message)
            else:
                return False
        except (InvalidSignature, ValueError):
            return False
        return abs(int(ts) - self.clock.ms()) <= 30_000

    def _route(self, method, sub, params, body):
        if method == "POST" and sub == "/portfolio/events/orders":
            return self._create(body)
        match = re.fullmatch(r"/portfolio/events/orders/([^/]+)", sub)
        if method == "DELETE" and match:
            return self._cancel(match.group(1), params)
        if method == "GET":
            if sub == "/portfolio/orders":
                return self._list_orders(params)
            match = re.fullmatch(r"/portfolio/orders/([^/]+)", sub)
            if match:
                return self._get_order(match.group(1))
            if sub == "/portfolio/fills":
                return self._list_fills(params)
            if sub == "/portfolio/positions":
                return self._positions(params)
            if sub == "/portfolio/balance":
                return self._balance()
            if sub == "/exchange/status":
                return self._status()
        self.unknown.append((method, sub))
        return self._error(404, "not_found")

    def _page(self, items, params, key):
        start = int(params.get("cursor") or 0)
        size = min(int(params.get("limit", 100)), self.page_size)
        page = items[start : start + size]
        if self.duplicate_pages and start > 0:
            page = [items[start - 1]] + page
        nxt = start + size
        return self._reply(200, {key: page, "cursor": str(nxt) if nxt < len(items) else ""})

    # -- orders --------------------------------------------------------------------

    def _order_json(self, o):
        return {
            "order_id": o["order_id"],
            "user_id": "synthetic-user",
            "client_order_id": o["client_order_id"] or "",
            "ticker": o["ticker"],
            "outcome_side": "yes" if o["book_side"] == "bid" else "no",
            "book_side": o["book_side"],
            "type": "limit",
            "status": o["status"],
            "yes_price_dollars": fp(o["yes_price"], 4),
            "no_price_dollars": fp(1 - o["yes_price"], 4),
            "fill_count_fp": fp(o["filled"], 2),
            "remaining_count_fp": fp(o["remaining"], 2),
            "initial_count_fp": fp(o["initial"], 2),
            "taker_fees_dollars": fp(o["taker_fees"], 6),
            "maker_fees_dollars": fp(o["maker_fees"], 6),
            "taker_fill_cost_dollars": "0.000000",
            "maker_fill_cost_dollars": "0.000000",
            "expiration_time": None,
            "created_time": iso(o["created"]),
            "last_update_time": iso(o["updated"]),
            "self_trade_prevention_type": o["stp"],
            "order_group_id": None,
            "cancel_order_on_pause": False,
            "subaccount_number": 0,
            "exchange_index": self.exchange_index,
        }

    def seed_order(self, client_order_id, ticker, book_side, yes_price, count):
        """A resting order created outside the adapter (stray or foreign)."""
        return self._new_order(
            client_order_id, ticker, book_side, D(yes_price), D(count), "taker_at_cross"
        )

    def _new_order(self, coid, ticker, side, yes, count, stp):
        self._orders_n += 1
        n = self._orders_n
        order_id = f"{n:08x}-0000-4000-8000-{n:012x}"
        now = self.clock.now()
        self.orders[order_id] = {
            "order_id": order_id,
            "client_order_id": coid,
            "ticker": ticker,
            "book_side": side,
            "yes_price": yes,
            "initial": count,
            "filled": D(0),
            "remaining": count,
            "status": "resting",
            "taker_fees": D(0),
            "maker_fees": D(0),
            "created": now,
            "updated": now,
            "stp": stp,
        }
        if self.lag:
            self.hidden_orders.add(order_id)
        return order_id

    def publish(self):
        self.hidden_orders.clear()
        self.hidden_fills.clear()

    def _create(self, body):
        if not isinstance(body, dict) or not _CREATE_REQUIRED <= set(body):
            return self._error(400, "missing_parameters")
        if set(body) - _CREATE_ALLOWED or body["side"] not in ("bid", "ask"):
            return self._error(400, "invalid_parameters")
        if not re.fullmatch(r"0\.\d{2,4}", body["price"]) or D(body["price"]) % D("0.01"):
            return self._error(400, "invalid_price")
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", body["count"]) or D(body["count"]) <= 0:
            return self._error(400, "invalid_order_size")
        tif = body["time_in_force"]
        if tif not in ("fill_or_kill", "good_till_canceled", "immediate_or_cancel"):
            return self._error(400, "invalid_parameters")
        if body["self_trade_prevention_type"] not in ("taker_at_cross", "maker"):
            return self._error(400, "invalid_parameters")
        if body.get("reduce_only") and tif != "immediate_or_cancel":
            return self._error(400, "invalid_parameters")
        if body.get("exchange_index") not in (None, self.exchange_index):
            return self._error(400, "wrong_exchange_index")
        if self.reject_create:
            code, self.reject_create = self.reject_create, None
            return self._error(400, code)
        coid = body.get("client_order_id")
        if coid and any(o["client_order_id"] == coid for o in self.orders.values()):
            return self._error(409, "order_already_exists")
        order_id = self._new_order(
            coid,
            body["ticker"],
            body["side"],
            D(body["price"]),
            D(body["count"]),
            body["self_trade_prevention_type"],
        )
        order = self.orders[order_id]
        for count, yes, fee in self.immediate(order) if self.immediate else ():
            self.execute(order_id, count, yes, is_taker=True, fee=fee)
        if tif == "immediate_or_cancel" and order["status"] == "resting":
            order["status"], order["remaining"] = "canceled", D(0)
        return self._reply(
            201,
            {
                "order_id": order_id,
                "client_order_id": coid,
                "fill_count": fp(order["filled"], 2),
                "remaining_count": fp(order["remaining"], 2),
                "ts_ms": self.clock.ms(),
            },
        )

    def _cancel(self, order_id, params):
        order = self.orders.get(order_id)
        if order is None or order_id in self.hidden_orders:
            return self._error(404, "not_found")
        if params.get("market_ticker") != order["ticker"]:
            return self._error(400, "market_ticker_required")
        if order["status"] != "resting":
            return self._error(404, "not_found")
        if self.on_cancel is not None:
            self.on_cancel(order)
            if order["status"] != "resting":
                return self._error(404, "not_found")
        reduced = order["remaining"]
        order["remaining"], order["status"] = D(0), "canceled"
        order["updated"] = self.clock.now()
        return self._reply(
            200,
            {
                "order_id": order_id,
                "client_order_id": order["client_order_id"],
                "reduced_by": fp(reduced, 2),
                "ts_ms": self.clock.ms(),
            },
        )

    def _visible_orders(self):
        return [o for o in self.orders.values() if o["order_id"] not in self.hidden_orders]

    def _list_orders(self, params):
        items = self._visible_orders()
        if "ticker" in params:
            items = [o for o in items if o["ticker"] == params["ticker"]]
        if "status" in params:
            items = [o for o in items if o["status"] == params["status"]]
        if "min_ts" in params:
            floor = EPOCH + timedelta(seconds=int(params["min_ts"]))
            items = [o for o in items if o["created"] >= floor]
        items.sort(key=lambda o: (o["created"], o["order_id"]), reverse=True)  # newest first
        return self._page([self._order_json(o) for o in items], params, "orders")

    def _get_order(self, order_id):
        order = self.orders.get(order_id)
        if order is None or order_id in self.hidden_orders:
            return self._error(404, "not_found")
        return self._reply(200, {"order": self._order_json(order)})

    # -- fills, positions, balance -----------------------------------------------------

    def execute(self, order_id, count, yes_price=None, *, is_taker=False, fee="0", hidden=False):
        """Fill `count` of a resting order at `yes_price` (default: its limit)."""
        order = self.orders[order_id]
        c = D(count)
        y = D(yes_price) if yes_price is not None else order["yes_price"]
        f = D(fee)
        assert order["status"] == "resting" and c <= order["remaining"]
        order["filled"] += c
        order["remaining"] -= c
        if order["remaining"] == 0:
            order["status"] = "executed"
        order["taker_fees" if is_taker else "maker_fees"] += f
        order["updated"] = self.clock.now()
        self._fills_n += 1
        fill_id = f"f{self._fills_n:07d}-synthetic"
        self.fill_log.append(
            {
                "fill_id": fill_id,
                "trade_id": fill_id,
                "order_id": order_id,
                "ticker": order["ticker"],
                "market_ticker": order["ticker"],
                "outcome_side": "yes" if order["book_side"] == "bid" else "no",
                "book_side": order["book_side"],
                "count_fp": fp(c, 2),
                "yes_price_dollars": fp(y, 6),
                "no_price_dollars": fp(1 - y, 6),
                "is_taker": is_taker,
                "created_time": iso(self.clock.now()),
                "fee_cost": fp(f, 6),
                "subaccount_number": 0,
                "exchange_index": self.exchange_index,
                "ts": (self.clock.now() - EPOCH) // timedelta(seconds=1),
            }
        )
        self._account(order["ticker"], order["book_side"], c, y, f)
        if hidden:
            self.hidden_fills.add(fill_id)
        return fill_id

    def _account(self, ticker, side, count, yes, fee):
        """Independent signed-position accounting (YES positive, NO negative)."""
        position = self.positions.get(ticker, D(0))
        if side == "bid":  # buy YES; closing NO pays $1 per netted pair
            closed_no = min(count, max(-position, D(0)))
            self.cash += -yes * count - fee + closed_no
            position += count
        else:  # sell YES / buy NO; opening NO costs (1 - yes) per contract
            opened_no = count - min(count, max(position, D(0)))
            self.cash += yes * count - fee - opened_no
            position -= count
        self.positions[ticker] = position

    def _list_fills(self, params):
        items = [f for f in self.fill_log if f["fill_id"] not in self.hidden_fills]
        if "order_id" in params:
            items = [f for f in items if f["order_id"] == params["order_id"]]
        if "min_ts" in params:
            floor = int(params["min_ts"])
            items = [f for f in items if f["ts"] >= floor]
        items = list(reversed(items))  # newest first
        return self._page(items, params, "fills")

    def _positions(self, params):
        rows = [
            {
                "ticker": ticker,
                "position_fp": fp(position, 2),
                "market_exposure_dollars": "0.0000",
                "realized_pnl_dollars": "0.0000",
                "fees_paid_dollars": "0.0000",
                "total_traded_dollars": "0.0000",
                "exchange_index": self.exchange_index,
            }
            for ticker, position in sorted(self.positions.items())
            if position != 0 or ticker in self.zero_position_rows
        ]
        return self._page(rows, params, "market_positions")

    def _balance(self):
        cents = int(self.cash * 100)
        return self._reply(
            200,
            {
                "balance": cents,
                "balance_dollars": fp(self.cash, 4),
                "portfolio_value": 0,
                "updated_ts": (self.clock.now() - EPOCH) // timedelta(seconds=1),
                "balance_breakdown": [
                    {"exchange_index": 0, "balance": 0},
                    {"exchange_index": 3, "balance": cents},
                ],
            },
        )

    def _status(self):
        shards = [
            {
                "exchange_index": i,
                "description": f"synthetic shard {i}",
                "exchange_active": True,
                "trading_active": True,
                "intra_exchange_transfers_active": True,
            }
            for i in range(4)
        ]
        return self._reply(
            200,
            {
                "exchange_active": True,
                "trading_active": True,
                "intra_exchange_transfers_active": True,
                "exchange_estimated_resume_time": None,
                "exchange_index_statuses": shards,
            },
        )

    def orders_with(self, client_order_id):
        return [o for o in self.orders.values() if o["client_order_id"] == client_order_id]


def live_ledger(path, cash="1000", limits=None, disengage=True):
    """A live ledger with an opening balance adjustment, complete limits and (by
    default) an explicit operator disengage of the kill switch."""
    ledger = Ledger(path, "live", created_at=at(-3600))
    ledger.append_cash_event(CashEvent("adjust-0", "adjustment", D(cash), at(-3600)))
    ledger.record_limits(limits or simulation_defaults(D(cash)), "test-operator", at(-3600))
    if disengage:
        ledger.set_kill_switch(False, "operator: synthetic test", "test-operator", at(-3600))
    return ledger


class Harness:
    """Fake clock + double + live ledger + KalshiGateway + Executor."""

    def __init__(self, path, *, cash="1000", key=None, ledger=None, **gateway_options):
        self.path = path
        self.clock = FakeClock()
        self.key = key or ed25519.Ed25519PrivateKey.generate()
        self.double = KalshiDouble(self.clock, self.key.public_key(), cash=cash)
        self.ledger = ledger or live_ledger(path, cash)
        self.gateway = self.make_gateway(**gateway_options)
        self.executor = Executor(self.ledger, self.gateway, RiskEngine("live"), FEES)

    def make_gateway(self, **options):
        signer = Signer(KEY_ID, self.key, clock_ms=self.clock.ms)
        client = KalshiClient(
            "demo",
            signer,
            transport=self.double.transport(),
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )
        settings = {
            "markets": (SYN_RULE,),
            "max_order_notional": "50",
            "max_daily_notional": "200",
            "fills_epoch": at(-3600),
            "now": self.clock.now,
            **options,
        }
        return KalshiGateway(client, ledger=self.ledger, **settings)

    def restart(self, **options):
        """A new process: fresh gateway memory and executor over the same ledger/venue."""
        self.ledger.close()
        self.ledger = Ledger(self.path, "live")
        self.gateway = self.make_gateway(**options)
        self.executor = Executor(self.ledger, self.gateway, RiskEngine("live"), FEES)

    def at(self, seconds):
        """Move the synthetic clock to T0 + seconds and return that instant."""
        target = at(seconds)
        if target > self.clock.value:
            self.clock.value = target
        return self.clock.value
