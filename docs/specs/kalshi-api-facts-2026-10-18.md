# Kalshi Trade API v2: Implementation Facts (paper simulator + live adapter)

**Retrieval date for every source below: 2026-10-18** (local, US time; UTC clock read 2026-10-19 ~04:00Z).
Only read-only documentation retrieval, plus 3 public unauthenticated GET market-data calls (L1-L3). No credentials, no orders.

**Tag legend**
- `[V:<key>]` = VERIFIED: quoted or closely paraphrased from that source, fetched 2026-10-18.
- `[INF]` = my inference or arithmetic from verified facts. Not stated by Kalshi.
- `[UNV]` = unverified (memory or third-party). Do not rely on it without checking.

## Sources (exact URLs, all retrieved 2026-10-18)

Doc pages were pulled as the Mintlify `.md` variants, which carry the same content as the HTML pages.

| Key | URL |
|---|---|
| ENV | https://docs.kalshi.com/getting_started/api_environments.md |
| DEMO | https://docs.kalshi.com/getting_started/demo_env.md |
| KEYS | https://docs.kalshi.com/getting_started/api_keys.md |
| QAUTH | https://docs.kalshi.com/getting_started/quick_start_authenticated_requests.md |
| QORD | https://docs.kalshi.com/getting_started/quick_start_create_order.md |
| QMD | https://docs.kalshi.com/getting_started/quick_start_market_data.md |
| QWS | https://docs.kalshi.com/getting_started/quick_start_websockets.md |
| RL | https://docs.kalshi.com/getting_started/rate_limits.md |
| FP | https://docs.kalshi.com/getting_started/fixed_point_migration.md ("Last Updated: August 20, 2026") |
| FR | https://docs.kalshi.com/getting_started/fee_rounding.md |
| HIST | https://docs.kalshi.com/getting_started/historical_data.md |
| TM | https://docs.kalshi.com/getting_started/targets_and_milestones.md |
| LC | https://docs.kalshi.com/getting_started/market_lifecycle.md |
| MP | https://docs.kalshi.com/getting_started/maintenance_and_pauses.md |
| SET | https://docs.kalshi.com/getting_started/market_settlement.md |
| SB | https://docs.kalshi.com/getting_started/settlement_bounds.md |
| SHARD | https://docs.kalshi.com/getting_started/exchange_sharding.md |
| OG | https://docs.kalshi.com/getting_started/order_groups.md |
| DIR | https://docs.kalshi.com/getting_started/order_direction.md |
| OB | https://docs.kalshi.com/getting_started/orderbook_responses.md |
| OAS | https://docs.kalshi.com/openapi.yaml (official OpenAPI spec, `info.version: 3.34.0`) |
| AAS | https://docs.kalshi.com/asyncapi.yaml (official AsyncAPI spec for WebSockets) |
| WSKA | https://docs.kalshi.com/websockets/connection-keep-alive.md |
| WSUO | https://docs.kalshi.com/websockets/user-orders.md |
| WSUF | https://docs.kalshi.com/websockets/user-fills.md |
| WSC | https://docs.kalshi.com/websockets/websocket-connection.md |
| BCS | https://docs.kalshi.com/api-reference/market/batch-get-market-candlesticks.md |
| CL | https://docs.kalshi.com/changelog/index.md (API changelog; some entries are future-dated, e.g. "October 22, 2026") |
| FEEPDF | https://kalshi.com/docs/kalshi-fee-schedule.pdf. **Direct fetch FAILED**: HTTP 429 "Vercel Security Checkpoint" on both curl and WebFetch. |
| FEEPDF-WB | https://web.archive.org/web/20260612075630id_/https://kalshi.com/docs/kalshi-fee-schedule.pdf. Wayback capture (2026-06-12) of the official PDF. PDF title: "Fee Schedule for Feb 2026 - 2.5.26 Update". |
| FEEPAGE | https://kalshi.com/fee-schedule. **FAILED**: HTTP 429 Vercel checkpoint. |
| HELPFEES | https://help.kalshi.com/en/articles/13823805-fees (WebFetch summary; page dated April 19, 2026) |
| L1 | Live public GET `https://external-api.kalshi.com/trade-api/v2/events?series_ticker=KXMLBGAME&limit=2&with_nested_markets=true&with_milestones=true&status=open` at 2026-10-19T04:07:49Z |
| L2 | Live public GET `https://external-api.kalshi.com/trade-api/v2/series/KXMLBGAME` at ~04:08:10Z |
| L3 | Live public GET `https://external-api.kalshi.com/trade-api/v2/historical/cutoff` at ~04:08:10Z |

---

## 1. Base URLs and authentication

**Base URLs**
- REST production (recommended): `https://external-api.kalshi.com/trade-api/v2`. Also supported: `https://api.elections.kalshi.com/trade-api/v2`. [V:ENV][V:OAS servers]
- REST demo (recommended): `https://external-api.demo.kalshi.co/trade-api/v2`. Also supported: `https://demo-api.kalshi.co/trade-api/v2`. [V:ENV][V:DEMO]
- WebSocket production: `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (also `wss://api.elections.kalshi.com/trade-api/ws/v2`). [V:ENV]
- WebSocket demo: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` (also `wss://demo-api.kalshi.co/trade-api/ws/v2`). [V:ENV]
- Demo and production credentials are separate. Demo keys only work on demo, production keys only on production. [V:ENV]
- Demo UI is at https://demo.kalshi.co/. "The price and behavior of markets in the demo environment may not be reflective of those in real markets." [V:DEMO]

**Key types**
- Two key types are supported:
  - Ed25519, the recommended type. PKCS#8 PEM.
  - RSA 2048-bit, RSA-PSS with SHA-256. Kalshi generates PKCS#1 PEM (`BEGIN RSA PRIVATE KEY`).
  [V:KEYS]
- "Headers, pre-sign text, permissions and errors are identical for both types." Kalshi picks the verification algorithm from the registered public key. [V:KEYS]
- The PEM header does not identify the key type. Detect the type from the parsed key object. [V:KEYS]

**Headers (all three are required on every authenticated request)**
- `KALSHI-ACCESS-KEY`: the Key ID (a UUID). [V:KEYS][V:QAUTH][V:OAS securitySchemes]
- `KALSHI-ACCESS-TIMESTAMP`: request timestamp in **milliseconds**. [V:KEYS][V:OAS]
- `KALSHI-ACCESS-SIGNATURE`: base64 of the signature over the pre-sign text. [V:KEYS][V:OAS]

**Signed string**
- Pre-sign text is `timestamp_ms + HTTP_METHOD + path`, for example `1703123456789GET/trade-api/v2/portfolio/balance`. [V:QAUTH]
- Sign the full path from the API root (`/trade-api/v2/...`), with no host and no query string.
  - For `.../portfolio/orders?limit=5`, sign `/trade-api/v2/portfolio/orders`. [V:KEYS][V:ENV]
  - The host never changes the payload. [V:ENV]
- The request body is not part of the signed string. [V: implied by KEYS "concatenation of the timestamp, the HTTP method and the path"; INF that bodies are excluded]
- RSA signing: RSA-PSS, SHA-256, MGF1(SHA-256), **salt length = digest length** (32 bytes). The samples use:
  - Python: `padding.PSS(mgf=MGF1(SHA256()), salt_length=PSS.DIGEST_LENGTH)`
  - Node: `RSA_PSS_SALTLEN_DIGEST`
  [V:KEYS]
- Ed25519 signs the UTF-8 message directly, with no separate hash. [V:KEYS]
- The Python and TypeScript SDKs accept either key type from v3.31.0. [V:KEYS]
- WebSocket handshake: the same three headers. Sign `timestamp + "GET" + "/trade-api/ws/v2"`. [V:QWS]
- Common error: "Signature error → Ensure timestamp is in milliseconds (not seconds)". [V:QAUTH]
- **Clock-skew tolerance: NOT documented** on KEYS, QAUTH, ENV or OAS (searched for "skew"/"clock"). [gap]

## 2. Orders

### 2.0 Endpoint generation: V2 is mandatory for writes
- Current write endpoints live under `/portfolio/events/orders`. They use the "V2 request/response shape (single-book `bid`/`ask` side and fixed-point dollar prices)". [V:OAS CreateOrderV2]
- The legacy `/portfolio/orders` mutation endpoints were deprecated "sometime between June 18 and June 25" 2026. Once deprecated, calls return `Please switch to the V2 endpoints`. [V:CL "June 18, 2026"]
  - Affected endpoints: `POST /portfolio/orders`, `DELETE /portfolio/orders/{id}`, `POST .../{id}/decrease`, `POST|DELETE /portfolio/orders/batched`, `POST .../{id}/amend`.
  - These paths are absent from the current OAS paths. Only `GET /portfolio/orders` and `GET /portfolio/orders/{order_id}` remain. [V:OAS]
- Legacy schemas still in the OAS: `CreateOrderRequest` (side yes/no, action buy/sell, count/count_fp, yes_price/no_price in cents 1-99, `*_price_dollars`, expiration_ts, buy_max_cost in cents, sell_position_floor). They are reference only. [V:OAS]
- Market orders were removed: "`POST /portfolio/orders` removed `type`; `type=market` is no longer offered" (Feb 11, 2026). [V:CL]
  - V2 has no `type` field and no `buy_max_cost`. [V:OAS CreateOrderV2Request]
  - The `Order` response object still has `type: [limit, market]`. [V:OAS]

### 2.1 Create order: `POST /portfolio/events/orders` → 201 [V:OAS]

Required fields: `ticker`, `side`, `count`, `price`, `time_in_force`, `self_trade_prevention_type`.

| Field | Values and meaning |
|---|---|
| `side` | `bid` = buy YES, `ask` = sell YES. "Selling YES is economically equivalent to buying NO at `1 - price`, but this endpoint quotes everything from the YES side." [V:OAS BookSide] |
| `count` | FixedPointCount string, for example `"10.00"`. |
| `price` | FixedPointDollars string, for example `"0.5600"`. This is always the YES price. [V:OAS] |
| `time_in_force` | One of `fill_or_kill`, `good_till_canceled`, `immediate_or_cancel`. `GTT` is not a valid API value. [V:OAS] |
| `expiration_time` | Optional, int64 Unix **seconds**. Use it with `good_till_canceled` for an expiring order. It cannot be combined with `immediate_or_cancel`. [V:OAS] |
| `self_trade_prevention_type` | Required. `taker_at_cross` cancels the taker order; partial fills already matched are executed. `maker` cancels the resting maker order and continues matching. [V:OAS] |
| `client_order_id` | Optional string. |
| `post_only` | Optional bool. A post-only order that would cross is canceled with `last_update_reason=PostOnlyCrossCancel`. [V:CL "Post Only Cross Cancel update reason added"] |
| `reduce_only` | Optional bool. "Orders with reduce_only set to true will be rejected unless time_in_force is immediate_or_cancel." [V:OAS] |
| `cancel_order_on_pause` | Optional bool. Cancels the order if trading or the exchange pauses. [V:OAS][V:MP] |
| `subaccount` | Optional int, 0 = primary. |
| `order_group_id` | Optional. |
| `exchange_index` | Optional int. If omitted, auto-routes by ticker. `-1` forces auto-routing. [V:OAS] |

- **Buy-NO mapping** [INF]: to buy NO at $0.40, send `side=ask, price="0.6000"`.
  - Supporting text [V:DIR]: legacy (buy, no) maps to `book_side=ask`, and "Direction does not change the price".
  - Not shown as an explicit example in the docs.

**Response (201)**
- `order_id`
- `client_order_id`
- `fill_count` (fp): contracts filled immediately.
- `remaining_count` (fp). For IOC orders this is the final state after unfilled contracts are canceled.
- `average_fill_price` (dollars, only when `fill_count` > 0)
- `average_fee_paid` (volume-weighted fee per contract, only when `fill_count` > 0)
- `ts_ms` (matching-engine time, epoch ms)
[V:OAS CreateOrderV2Response]

**Error statuses:** 400, 401, 403, 409, 429, 500, 503. [V:OAS]

### 2.2 Order statuses
- REST `OrderStatus`: `resting`, `canceled`, `executed`. There is **no `pending`**. [V:OAS]
- WS `user_order.status`: `resting`, `canceled`, `executed`, `unknown`. [V:AAS userOrderPayload]
- Last-update reasons (WS `last_update_reason`): `Decrease`, `Amend`, `MarginCancel`, `SelfTradeCancel`, `ExpiryCancel`, `CloseCancel`, `HaltCancel`, `Trade`, `PostOnlyCrossCancel`, `ReduceOnlyCancel`, `SettlementBoundsCancel`. [V:AAS lastUpdateReason]

### 2.3 `client_order_id` uniqueness and idempotency
- QORD says:
  - "Generate a unique ID (like UUID4) for each order before submission when you want idempotent retries."
  - "If network issues occur, you can resubmit with the same `client_order_id`."
  - "The API will reject duplicate submissions with the same `client_order_id`."
  - Error list: "`409 Conflict`: Order with this `client_order_id` already exists."
  [V:QORD]
- FIX equivalent: `Text<58>=ORDER_ALREADY_EXISTS`, `CxlRejReason<102>=6` (Duplicate ClOrdID). [V:CL]
- The REST JSON `code` string for a duplicate is **not documented**. It is probably `order_already_exists`. [UNV]
- **Not documented:**
  - the uniqueness scope: forever, per open order, or per market or subaccount;
  - whether a 409 response body includes the existing `order_id`;
  - any length or format limits.
  [gap]
- A 409 alone does not tell you the original's state. [INF] After a 409, locate the order by listing `GET /portfolio/orders?ticker=...&min_ts=...` and matching `client_order_id` client-side, or from WS `user_orders` (each message carries `client_order_id`).
- **No `client_order_id` query filter** on `GET /portfolio/orders`. [V:OAS params]
  - Only `GET /fcm/orders` accepts `client_order_ids`, and it is FCM-only. [V:OAS][V:CL]

### 2.4 Other order endpoints

**Cancel: `DELETE /portfolio/events/orders/{order_id}`**
- Query: `subaccount`, `exchange_index`, `market_ticker`. Auto-routing needs `market_ticker`.
- Returns 200 `{order_id, client_order_id, reduced_by (fp), ts_ms}`. 404 if not found. Costs 2 tokens.
[V:OAS]

**Cancel all: `DELETE /portfolio/events/orders?subaccount=`**
- Returns 204. Covers all shards.
- "Newly placed orders may also be cancelled during the minute after the request." Costs 2 tokens.
[V:OAS]

**Amend: `POST /portfolio/events/orders/{order_id}/amend`**
- Body: `ticker`, `side`, `price`, `count`, `client_order_id`, `updated_client_order_id`, `expiration_time`, `exchange_index`.
  - Required: `ticker`, `side`, `price`, `count`.
  - `count` is the **total** (already filled + desired resting).
  - `expiration_time`: omit to keep the current expiry, `0` to clear it, or a future Unix-seconds value.
- Queue priority: "Amending only expiry or decreasing size preserves queue position. Increasing size or changing price forfeits queue position."
- Response: `{order_id, client_order_id, remaining_count, fill_count, average_fill_price, average_fee_paid, ts_ms}`.
[V:OAS][V:CL Oct 1 2026]

**Decrease: `POST /portfolio/events/orders/{order_id}/decrease`**
- Body: exactly one of `reduce_by` or `reduce_to` (fp), plus `exchange_index` and `market_ticker`.
- Response: `{order_id, client_order_id, remaining_count, ts_ms}`.
[V:OAS]

**Batch create: `POST /portfolio/events/orders/batched`**
- Body: `{orders:[CreateOrderV2Request...]}`. Returns 201.
- Each result item: `{order_id, client_order_id, fill_count, remaining_count, average_fill_price, average_fee_paid, ts_ms, error}`.
- Billing is 10 tokens per item, always charged to the unscoped Write bucket.
- Max batch size "scales with your tier's write budget". The whole batch must fit in the bucket, or the entire batch is rejected.
[V:OAS][V:RL]

**Batch cancel: `DELETE /portfolio/events/orders/batched`**
- Body: `{orders:[{order_id, subaccount, exchange_index, market_ticker}]}`. Returns 200.
- Each result item: `{order_id, client_order_id, reduced_by, ts_ms, error}`. Costs 2 tokens per item.
[V:OAS]

**Get order: `GET /portfolio/orders/{order_id}`**
- Returns `{order: Order}`. 404 if not found. Costs 2 tokens.
[V:OAS]

**Order object fields**
- `order_id`, `user_id`, `client_order_id`, `ticker`
- `outcome_side` (yes/no), `book_side` (bid/ask), `type`, `status`
- `yes_price_dollars`, `no_price_dollars`
- `fill_count_fp`, `remaining_count_fp`, `initial_count_fp`
- `taker_fill_cost_dollars`, `maker_fill_cost_dollars`, `taker_fees_dollars`, `maker_fees_dollars`
- `expiration_time`, `created_time`, `last_update_time`
- `self_trade_prevention_type`, `order_group_id`, `cancel_order_on_pause`, `subaccount_number`, `exchange_index`
- Deprecated: `side`, `action`
[V:OAS Order]

Deprecation dates for the legacy direction fields conflict:
- OAS says "will not be removed before May 14, 2026".
- DIR says "will not be removed before May 28, 2026".
- Both dates have passed, but the fields are still in the spec. [V:OAS][V:DIR]

**List orders: `GET /portfolio/orders`**
- Filters:
  - `ticker`
  - `event_ticker` (comma-separated, max 10)
  - `min_ts` / `max_ts` (Unix)
  - `status` (`resting|canceled|executed`)
  - `limit` (1-1000, default 100)
  - `cursor`
  - `subaccount` (omitted = all subaccounts)
  - `exchange_index`
- Canceled or executed orders older than `orders_updated_ts` appear only in `GET /historical/orders`. Resting orders always appear.
[V:OAS][V:HIST]

### 2.5 Fills: `GET /portfolio/fills` [V:OAS]
- Filters:
  - `ticker` (comma list, up to 100)
  - `order_id`
  - `min_ts`, `max_ts`
  - `limit` (1-1000), `cursor`
  - `subaccount`, `exchange_index`
- Fill fields:
  - `fill_id`; `trade_id` (legacy name, same value as `fill_id`)
  - `order_id`
  - `ticker`; `market_ticker` (legacy)
  - `outcome_side`, `book_side`
  - `count_fp`, `yes_price_dollars`, `no_price_dollars`
  - `is_taker`
  - `created_time` (RFC3339)
  - `fee_cost` (fixed-point dollars)
  - `subaccount_number`, `exchange_index`
  - `ts` (legacy Unix)
  - Deprecated: `side`, `action`
- The REST Fill has **no `client_order_id`**. The WS `fill` message does carry one (optional).
- Fills older than `trades_created_ts` are only available from `GET /historical/fills`. [V:HIST]

### 2.6 Positions: `GET /portfolio/positions` [V:OAS]
- Query parameters:
  - `cursor`, `limit` (1-1000)
  - `count_filter` (`position`, `total_traded`)
  - `settlement_status` (`unsettled` by default, `settled`, `all`)
  - `ticker`, `event_ticker`
  - `subaccount` (default 0), `exchange_index`
- `market_positions[]` fields:
  - `ticker`, `exchange_index`
  - `total_traded_dollars`
  - `position_fp`: negative = NO, positive = YES
  - `market_exposure_dollars`
  - `realized_pnl_dollars`
  - `fees_paid_dollars`
  - `last_updated_ts`
- `event_positions[]` fields: `event_ticker`, `total_cost_dollars`, `total_cost_shares_fp`, `event_exposure_dollars`, `realized_pnl_dollars`, `fees_paid_dollars`.
- **`resting_orders_count` was removed** (July 9, 2026). [V:CL]

### 2.7 Balance: `GET /portfolio/balance` [V:OAS][V:CL Oct 15 2026][V:RL]
- Fields:
  - `balance`: int64, **cents**. It truncates sub-cent amounts.
  - `balance_dollars`: fixed-point string. Use this one for exact values.
  - `portfolio_value`: int64 cents.
  - `updated_ts`: Unix.
  - `balance_breakdown[]`: `{exchange_index, balance}`.
- Query: `subaccount`, `exchange_index`. Omitting `exchange_index` sums all shards.
- Direct-member balances are aligned to $0.0001. Non-direct (FCM) balances are aligned to $0.01. [V:FR][V:CL May 28 2026]
- Balance has its own rate bucket: 1 request/s on Basic and Advanced, 5/s on higher tiers, with no burst.

### 2.8 Settlements: `GET /portfolio/settlements` [V:OAS]
- Query: `limit`, `cursor`, `ticker`, `event_ticker`, `min_ts`, `max_ts`, `subaccount`.
- Fields:
  - `ticker`, `exchange_index`, `event_ticker`
  - `market_result` (`yes|no|scalar`)
  - `yes_count_fp`, `yes_total_cost_dollars`, `no_count_fp`, `no_total_cost_dollars`
  - `revenue`: int **cents**
  - `settled_time`
  - `fee_cost`: dollars string
  - `value`: int cents, the payout of one YES contract (nullable)
- Only live records are returned. After archival there is no historical endpoint with the same settlement fields. `GET /historical/positions` has position, P&L and fee data. [V:HIST]

## 3. Queue position and order groups
- `GET /portfolio/orders/{order_id}/queue_position` returns `{queue_position_fp}`, "the number of preceding shares before the order in the queue" (price-time priority). [V:OAS]
- `GET /portfolio/orders/queue_positions?market_tickers=a,b&event_ticker=&subaccount=` returns `{queue_positions:[{order_id, market_ticker, queue_position_fp}]}` for all resting orders. [V:OAS]
- Order group endpoints [V:OAS]:
  - `GET /portfolio/order_groups`
  - `POST /portfolio/order_groups/create`
  - `GET|DELETE /portfolio/order_groups/{id}`
  - `PUT /portfolio/order_groups/{id}/reset`
  - `PUT /portfolio/order_groups/{id}/trigger`
  - `PUT /portfolio/order_groups/{id}/limit`
- Order group behavior:
  - Contracts limit is 1-1,000,000, measured over a rolling 15-second window.
  - When the limit is hit, all resting orders in the group are canceled and new orders are rejected until a reset.
  - Up to 100,000 groups per user.
  - "Order groups do not function across exchange instances."
  [V:OG][V:OAS][V:SHARD]

## 4. WebSocket (orders and fills)
- One connection. Auth headers are sent at the handshake; the connection itself requires auth even for public channels. [V:WSC][V:QWS]
- Channel names (subscribe enum): `orderbook_delta`, `ticker`, `trade`, `fill`, `market_positions`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `communications`, `order_group_updates`, `user_orders`, `cfbenchmarks_value`, `cfbenchmarks_value_5hz`, `pyth_value`. [V:WSC]
- Commands: `subscribe`, `unsubscribe`, `list_subscriptions`, `update_subscription` (actions `add_markets` / `delete_markets`; target `sid` or `sids`). [V:AAS][V:WSUO]
  - Each command has a client `id`, unique per session.
  - Subscribe params include `market_ticker` / `market_tickers` / `market_id(s)`, `send_initial_snapshot`, `skip_ticker_ack`, and `use_yes_price` (orderbook only).
- **`user_orders`** (message type `user_order`):
  - Market filter is optional; omit it to receive all orders.
  - Fields:
    - `order_id`, `user_id`, `ticker`, `exchange_index`, `status`
    - `outcome_side`, `book_side`
    - `yes_price_dollars`
    - `fill_count_fp`, `remaining_count_fp`, `initial_count_fp`
    - `taker_fill_cost_dollars`, `maker_fill_cost_dollars`, `taker_fees_dollars`, `maker_fees_dollars` (6 dp)
    - `client_order_id`, `order_group_id`, `self_trade_prevention_type`
    - `created_ts_ms`, `last_updated_ts_ms`, `expiration_ts_ms`
    - `last_update_reason`, `subaccount_number`
    - Deprecated: `side`, `is_yes`
  [V:AAS][V:WSUO]
- **`fill`** (message type `fill`):
  - Fields:
    - `trade_id`, `order_id`, `client_order_id` (optional)
    - `market_ticker`, `exchange_index`
    - `is_taker`, `outcome_side`, `book_side`
    - `yes_price_dollars`, `count_fp`
    - `fee_cost` (dollars, microcent precision)
    - `post_position_fp`
    - `ts_ms`; `ts` (seconds, deprecated)
    - `subaccount`
    - Deprecated: `side`, `action`, `purchased_side`
  - "Updates sent immediately when your orders are filled."
  [V:AAS][V:WSUF]
- **`market_positions`** (message type `market_position`): `user_id`, `market_ticker`, `position_fp`, `position_cost_dollars`, `realized_pnl_dollars`, `fees_paid_dollars`, `position_fee_cost_dollars`, `volume_fp`, `subaccount`. [V:AAS]
- **`order_group_updates`**: has `seq`. `event_type` is one of `created`, `triggered`, `reset`, `deleted`, `limit_updated`. [V:AAS]
- **Sequence numbers**
  - `seq` is "Sequential number that should be checked if you want to guarantee you received all the messages. Used for snapshot/delta consistency." [V:AAS]
  - Sequenced channels: orderbook snapshot/delta, trade, market and event lifecycle, event fee update, RFQ/quote, and order_group_updates. [V:CL Sept 10 2026][V:AAS]
  - The `fill`, `user_order` and `market_position` payload schemas have **no `seq` field**. [V:AAS]
  - [INF] Do not expect gap detection on fills or orders; reconcile through REST.
- Every subscription has a server `sid`. Messages carry `sending_ts_ms` (when Kalshi queued the message), added Oct 1, 2026. [V:AAS][V:CL]
- Keep-alive: Kalshi sends Ping every 10 s with body `heartbeat`. Clients should Pong, and may send their own Pings. [V:WSKA]
- Error codes include:
  - 9: Authentication required
  - 18: Command timeout
  - 25: Subscription buffer overflow
  [V:QWS][V:AAS]
- Maintenance: expect disconnects during Thursday 3-5 AM ET and reconnect after 5:00 AM ET. [V:MP]

## 5. Rate limits [V:RL unless noted]
- Token buckets. Most requests cost **10 tokens**. `GET /account/endpoint_costs` is the authoritative list of non-default costs.
  - Documented non-default costs: GetOrder 2, cancel 2, cancel-all 2, batch cancel 2 per item, batch create 10 per item, API usage-level upgrade 30. [V:OAS]
- There are two buckets per user:
  - **Read**: GETs and anything not routed to Write.
  - **Write**: order place, amend, cancel, order groups, RFQ quote flow, block-trade accepts.
- REST and FIX draw from the same buckets.

Per-second budgets (tokens/s, Read / Write):

| Tier | Read | Write |
|---|---|---|
| Basic | 200 | 100 |
| Advanced | 300 | 300 |
| Expert | 600 | 600 |
| Premier | 1,200 | 1,200 |
| Paragon | 2,400 | 2,400 |
| Prime | 4,800 | 4,800 |
| Prestige | 12,000 | 9,600 |

- [INF] At the default 10-token cost, Basic Write sustains about 10 orders/s and Advanced about 30/s.
- **Burst capacity**
  - Holds 3 seconds of budget: Basic and Advanced Read buckets, and Write buckets above Basic.
  - Holds 1 second only: Basic Write, Read above Advanced.
  - [INF] A Basic Write bucket (capacity 100) can therefore hold at most a 10-order batch.
- **Tiers**
  - Basic: granted at signup.
  - Advanced: call `POST /account/api_usage_level/upgrade`.
  - Higher tiers: earned by 30-day volume share, or assigned by Kalshi.
- `GET /account/limits` returns `usage_tier`, `read{refill_rate,bucket_capacity}`, `write{...}` and `grants[]`.
- **Shard billing**
  - Single REST writes with `exchange_index >= 1` bill that shard's Write bucket.
  - Explicit shard 0 bills the unscoped bucket.
  - **Auto-routed writes bill every shard's Write bucket.**
  - Batch writes always bill the unscoped bucket.
- **On limit**: HTTP **429** with body `{"error": "too many requests"}`.
  - "429 responses do not currently include `Retry-After` or `X-RateLimit-*` headers. There is no penalty or cooldown."
  - "Apply exponential backoff on 429."
- Legacy order-endpoint costs were raised to 10x V2 (for example legacy `POST /portfolio/orders` cost 100). [V:CL] Irrelevant now that the legacy endpoints are deprecated.
- Limits for unauthenticated public requests: **not documented**. [gap]
  - L1 response headers showed `cache-control: public, max-age=15`, and no rate-limit headers. [V:L1]

## 6. Price and quantity precision [V:FP][V:OAS]
- **Prices** are `*_dollars` fixed-point strings.
  - Requests accept 2-4 decimals (for example `"0.56"`, `"0.5600"`). Responses emit up to 6.
  - Integer-cent fields cannot represent sub-cent prices.
  - The legacy integer price and count fields were removed in March 2026. [V:CL Mar 2026 entries]
- **Counts** are `*_fp` strings.
  - Requests accept 0-2 decimals. Responses always emit 2.
  - **Minimum granularity is 0.01 contracts.** Fractional trading is on for all markets.
  - `fractional_trading_enabled` was deprecated as always-true, then removed July 9, 2026. [V:CL]
  - Kalshi suggests integer math: multiply `_fp` by 100.
- **Minimum order size:** not stated explicitly. [INF] 0.01 contracts, from the granularity rule; the legacy integer `count` had minimum 1. Error code `invalid_order_size` exists. [V:CL]
- `price_ranges` (array of `{start, end, step}` dollar strings) "is the source of truth for valid prices". Off-grid prices are rejected.
- `price_level_structure` is a display label only: "Do not key pricing logic off this name."

| Structure | Ranges and ticks |
|---|---|
| `linear_cent` | $0.01 everywhere |
| `deci_cent` | $0.001 everywhere |
| `tapered_deci_cent` | $0.001 below $0.10, $0.01 from $0.10 to $0.90, $0.001 above $0.90 |
| `center_whole_edge_half_cent` | $0.005 / $0.01 / $0.005 |
| `center_whole_edge_quint_cent` | $0.002 / $0.01 / $0.002 |
| `center_half_edge_half_cent` | $0.005 uniform |
| `center_half_edge_quint_cent` | $0.002 / $0.005 / $0.002 |
| `center_half_edge_deci_cent` | $0.001 / $0.005 / $0.001 |
| `center_quint_edge_quint_cent` | $0.002 uniform |
| `center_quint_edge_deci_cent` | $0.001 / $0.002 / $0.001 |
| `center_centi_edge_centi_cent` | $0.0001 uniform |
| `center_deci_edge_centi_cent` | $0.0001 below $0.01, $0.001 from $0.01 to $0.99, $0.0001 above $0.99 |

- Edges are at $0.10 / $0.90 for all tapered structures except `center_deci_edge_centi_cent`, which tapers at $0.01 / $0.99.
- Combos use `center_deci_edge_centi_cent`.
- Structure changes are announced as `price_level_structure_updated` on `market_lifecycle_v2`.
- Invalid prices: "off-tick, out-of-band, `$0`, or `$1`" are rejected (FIX `INVALID_PRICE`). [V:CL] [INF] REST has the same validity rules.
- Observed live: MLB moneyline market `KXMLBGAME-26OCT192008CLETB-CLE` has `price_level_structure: "linear_cent"` and `price_ranges: [{"start":"0.0000","end":"1.0000","step":"0.0100"}]`. [V:L1]
- REST orderbook: `GET /markets/{ticker}/orderbook?depth=0..100` (public).
  - Returns `{orderbook_fp:{yes_dollars:[[price,count_fp]...], no_dollars:[...]}}`. These are **bids only**.
  - Arrays are sorted ascending, so the best bid is the **last** element.
  - YES ask = 1 − best NO bid.
  - Batch version: `GET /markets/orderbooks?tickers=` (up to 100).
  [V:OB][V:OAS]

## 7. Fees

**Fee schedule PDF**
- Official URL: **https://kalshi.com/docs/kalshi-fee-schedule.pdf** (also linked from HELPFEES; page https://kalshi.com/fee-schedule).
  - Direct retrieval was **blocked (HTTP 429, Vercel checkpoint)** on 2026-10-18.
  - A search-engine index lists its current title as **"Fee Schedule for July 2026 - 7.7.26 Update"** (effective July 7, 2026). [UNV: title seen only in WebSearch results; content not retrieved]
- Verified content comes from the **Feb 5, 2026 version** (title "Fee Schedule for Feb 2026 - 2.5.26 Update", "Last updated and effective: Feb 5, 2026"), via Wayback capture 2026-06-12. [V:FEEPDF-WB]

**Feb 5, 2026 PDF content** [V:FEEPDF-WB]
- General (taker) fee: `fees = round up(0.07 x C x P x (1-P))`.
  - P = price in dollars, C = contracts.
  - "round up = rounds to the next cent".
- Trading fees are charged only on orders immediately matched, unless the market is in the "Maker Fees" section.
- Maker fees: `fees = round up(0.0175 x C x P x (1-P))`.
  - Charged only when a resting order executes. There is no fee for canceling.
  - "Please refer to https://kalshi.com/fee-schedule for up-to-date information on which markets have maker fees."
- Maker-fee rounding overpayment is reimbursed in the first week of the following month if it exceeds $10.
- S&P500 (INX*) and Nasdaq-100 (NASDAQ100*) markets use a Specific table: `round up(0.035 x C x P x (1-P))`.
- **"There is no settlement fee."** No membership fee.
- Card deposits: up to 2%. ACH: free.
- Sample General table values: 100 contracts at $0.50 → $1.75; 1 contract at $0.50 → $0.02; 100 at $0.01 → $0.07.

**Current docs on fee structure** [V:OAS Series.fee_type]
- `fee_type` values:
  - `quadratic`: General table.
  - `quadratic_with_maker_fees`: General table plus Maker Fees section.
  - `quadratic_with_combo_maker_fees`: "same maker-fee structure with a 0.5 maker multiplier instead of 0.25".
  - `flat`: Specific table.
- `fee_multiplier`: "a floating point multiplier applied to the fee calculations".
- The same fields appear on events via `fee_type_override` / `fee_multiplier_override`. Scheduled changes: `GET /series/fee_changes`, `GET /events/fee_changes`, and the `event_fee_update` WS message.
- [INF] The standard maker multiplier of 0.25 matches 0.0175 = 0.25 × 0.07.

**Live MLB values** [V:L2]
- `KXMLBGAME` series: `fee_type: "quadratic_with_maker_fees"`, `fee_multiplier: 0.5`, `exchange_index: 3`, tags `["Baseball"]`.
- [INF, not documented] If `fee_multiplier` scales the 0.07 coefficient (as 0.035 = 0.5 × 0.07 for INX), then for MLB games:
  - taker ≈ 0.035·C·P(1−P)
  - maker ≈ 0.25 × that ≈ 0.00875·C·P(1−P)
- **The exact formula linking `fee_multiplier` to the coefficient is NOT documented in pages I could fetch.** Verify against `average_fee_paid` / `fee_cost` on demo fills.

**Combo fee changes** [V:CL Aug 20/22 2026]
- "Maker fees will be enabled at 5:00 AM ET on Thursday, August 20".
- Combo quoter fee swap: if an order rested for less than 5 s, the quoter pays the maker fee (multiplier 0.5) and the resting side pays the taker fee.
- Independent NFL-only combos have no maker fee.

**Current rounding mechanics** [V:FR]
These supersede the PDF's "next cent" wording for sub-cent and fractional cases.
- `trade_fee = ceil_6dp(model_fee)`
- `aligned_change = floor_precision(revenue - trade_fee)`
- `rounding_fee = (revenue - trade_fee) - aligned_change`
- Precision is $0.0001 for direct members and $0.01 for non-direct (FCM).
- A per-order fee accumulator rebates the overpayment in precision increments. Net fee is never below zero.
- Worked example: revenue −$0.055, model fee $0.00363825 → trade fee $0.003639, aligned change −$0.06, rounding fee $0.001361.
  - [INF] $0.00363825 = 0.07 × 1 × 0.055 × 0.945, so the docs still use the 0.07·C·P(1−P) model fee.
- [INF] For a direct member, the effective fee is rounded to $0.0001, not to the cent, and accumulated per order.
  - Simulator option A: model `ceil(model_fee, $0.0001)` per fill.
  - Simulator option B: conservatively use ceil to the cent per order, matching the PDF.
- Settlement: "Settlement fees are zero for simple yes/no determinations but may apply for sub-cent scalar settlement." The payout is rounded to whole cents. [V:SET]
- Sports-specific taker rate: **not verified** beyond the MLB `fee_multiplier: 0.5` observation. [gap]

## 8. Public market data history

**Market candlesticks: `GET /series/{series_ticker}/markets/{ticker}/candlesticks`**
- **No auth**: no security block in the OAS.
- `start_ts` and `end_ts` are required, in Unix seconds.
  - start: candles ending on or after this time; end: candles ending on or before.
- `period_interval` is required and must be one of **1, 60, 1440** (minutes).
- Optional `include_latest_before_start`.
[V:OAS]
- Response `{ticker, candlesticks:[...]}`. Each candle has:
  - `end_period_ts`
  - `yes_bid{open_dollars,low_dollars,high_dollars,close_dollars}`
  - `yes_ask{same}`
  - `price{open_dollars,low_dollars,high_dollars,close_dollars,mean_dollars,previous_dollars,min_dollars,max_dollars}` (nullable when there were no trades)
  - `volume_fp`, `open_interest_fp`
  [V:OAS MarketCandlestick]

**Batch candlesticks: `GET /markets/candlesticks?market_tickers=a,b,...`**
- Up to 100 tickers and 10,000 candlesticks total.
- `start_ts` / `end_ts` in Unix seconds. `period_interval` is an int ≥ 1 with no enum in the spec.
- The spec lists a 401 response, so auth status is ambiguous: no security block, but 401 documented.
[V:BCS][V:OAS]

**Event candlesticks: `GET /series/{s}/events/{e}/candlesticks`**
- Returns `adjusted_end_ts`. Changelog: when more than 5000 candles, paginate using `adjustedEndTs` as the next `start_ts`. [V:OAS][V:CL Nov 2025]

**Maximum span**
- Max start/end span or candle count for single-market candlesticks: **not documented**. [gap]

**Historical candlesticks: `GET /historical/markets/{ticker}/candlesticks`**
- Public, same parameters. Applies to markets settled before `market_settled_ts`.
- **Field names differ** from the live endpoint:
  - `yes_bid{open,low,high,close}`, `yes_ask{...}`
  - `price{open,low,high,close,mean,previous}` (`mean` is VWAP)
  - `volume`, `open_interest` (fp strings)
[V:OAS MarketCandlestickHistorical]

**Trades: `GET /markets/trades`**
- **Public.** Query: `limit` (≤1000, default 100), `cursor`, `ticker`, `min_ts`, `max_ts` (Unix), `is_block_trade`.
- Trade fields: `trade_id`, `ticker`, `count_fp`, `yes_price_dollars`, `no_price_dollars`, `taker_outcome_side`, `taker_book_side`, `created_time`, `is_block_trade`. Deprecated: `taker_side`.
[V:OAS]

**Historical tier** [V:HIST][V:OAS]
- `GET /historical/cutoff` (public) returns `market_settled_ts`, `trades_created_ts`, `orders_updated_ts`, `market_positions_last_updated_ts` (RFC3339).
- Public endpoints:
  - `GET /historical/markets` (filters mutually exclusive: tickers / event_ticker / series_ticker / mve_filter)
  - `/historical/markets/{ticker}`
  - `/historical/markets/{ticker}/candlesticks`
  - `/historical/trades`
- Authenticated endpoints: `/historical/fills`, `/historical/orders`, `/historical/positions`.
- Live endpoints stop returning records older than the cutoff. Events and Series always stay on the live endpoints.
- `GET /events?with_nested_markets=true` omits archived markets.

**Cutoff values observed on 2026-10-19 04:08Z** [V:L3]
- `market_settled_ts` = `trades_created_ts` = `market_positions_last_updated_ts` = 2026-08-19T00:00:00Z
- `orders_updated_ts` = 2026-10-04T00:00:00Z
- [INF] Markets settled before Aug 19, 2026 and their candles and trades must come from `/historical/*`.

**How far back data goes:** the earliest available date for historical data is **not documented**. [gap]

## 9. Exchange status, schedule, market lifecycle

**Exchange endpoints** (public, no security in OAS)
- `GET /exchange/status` returns:
  - `exchange_active`, `trading_active`
  - `intra_exchange_transfers_active`
  - `exchange_estimated_resume_time` (nullable)
  - `exchange_index_statuses[]`: `{exchange_index, description, exchange_active, trading_active, intra_exchange_transfers_active}`
  - Possible statuses: 200, 500, 503, 504.
- `GET /exchange/schedule` returns `{schedule:{standard_hours:[WeeklySchedule], maintenance_windows:[{start_datetime,end_datetime}]}}`.
  - Daily sessions are `{open_time, close_time}` in ET, format "HH:MM".
- `GET /exchange/user_data_timestamp` returns `{as_of_time}`.
  - "There is typically a short delay before exchange events are reflected in the API endpoints." It covers GetBalance, GetOrder(s), GetFills and GetPositions.
[V:OAS]

**Maintenance** [V:MP]
- Every **Thursday 3:00-5:00 AM ET** a trading pause is in effect.
  - No placing or amending orders. Cancels are allowed.
  - Resting orders stay unless `cancel_order_on_pause` is set.
- A rare full exchange pause also blocks cancels.

**Market statuses (REST)** [V:LC][V:OAS Market.status]
- `initialized` → `active` (implicit at `open_time`) → `closed` (at `close_time`) → `determined` → `finalized`.
- Other states: `inactive` (paused), `disputed`, `amended`.
- `GET /markets?status=` filter values map as follows: `unopened`→initialized, `open`→active, `paused`→inactive, `closed`→past close and not finalized, `settled`→finalized.
- Reactivation from `inactive` "cancels all resting orders".

**Orders after close** [V:LC]
- After `close_time`, "all order operations, including cancellations, are rejected with `MARKET_INACTIVE`".
- Resting orders are canceled shortly after close.

**Settlement fields on Market** [V:OAS][V:LC]
- `result` ∈ {`yes`, `no`, `scalar`, `""`}
- `settlement_value_dollars` (after determination)
- `settlement_ts` (settled markets only)
- `expiration_value`
- `settlement_timer_seconds`: the dispute window after determination.
  - [V:L1] Observed value for MLB: 120.
- `settlement_bounds_type` (`default|floor`) and `settlement_floor_dollars`.
  - Settlement floors are "not live yet". [V:SB]
- Time fields: `open_time`, `close_time`, `expected_expiration_time`, `latest_expiration_time`, `occurrence_datetime`. `expiration_time` is deprecated. `can_close_early`.
- WS lifecycle (`market_lifecycle_v2`) events: `created`, `activated`, `deactivated`, `close_date_updated`, `determined`, `settled`, `metadata_updated`. Also emits `event_lifecycle` and `event_fee_update`. [V:LC]

## 10. Idempotent retries and timeouts
- The only official guidance:
  - Use a unique `client_order_id` per order.
  - On network issues, resubmit with the same `client_order_id`. Duplicates are rejected with 409.
  [V:QORD]
- Kalshi also advises combining write responses with WebSocket data because REST reads lag. [V:OAS user_data_timestamp]
- 429: exponential backoff, no Retry-After. [V:RL]
- `GET /markets/{ticker}` may 404 right after a `created` event: "Retry with backoff". [V:LC]
- No documented guidance on HTTP timeouts, 5xx handling for order POSTs, or the duplicate window. [gap]
- [INF] Retry recipe:
  1. On a timeout or 5xx, resend the identical body with the same `client_order_id`.
  2. If you get a 409, reconcile with `GET /portfolio/orders?ticker=&min_ts=` (match `client_order_id`), or with the `user_orders` stream.
  3. Never generate a new `client_order_id` for a retry of the same intent.
- Other write paths with explicit client IDs: intra-account transfers accept `client_transfer_id` ("Reuse only for identical requests"). [V:CL Oct 15 2026] This is not orders.

## 11. Sports milestones and structured targets (MLB)

**Endpoint auth and parameters**
- `GET /milestones`, `GET /milestones/{id}`, `GET /structured_targets`, `GET /structured_targets/{id}` have **no security requirement in the OAS** (public). [V:OAS]
  - Live call L1 (which embeds milestones via `/events?with_milestones=true`) succeeded unauthenticated. [V:L1]
- `GET /milestones` parameters:
  - `limit` **required**, 1-500
  - `minimum_start_date` (RFC3339)
  - `category` (e.g. `Sports`)
  - `competition` (e.g. `Pro Baseball`)
  - `source_id`
  - `type` (e.g. `baseball_game`)
  - `related_event_ticker`
  - `cursor`
  - `min_updated_ts` (Unix seconds, for polling)
  [V:OAS]
- Milestone fields: `id`, `category`, `type`, `start_date` ("Start date of the milestone"), `end_date`, `related_event_tickers`, `primary_event_tickers`, `title`, `notification_message`, `source_id`, `source_ids`, `details` (flexible JSON), `last_updated_ts`. [V:OAS][V:TM]

**Live MLB example** [V:L1]
- Event `KXMLBGAME-26OCT192008CLETB` ("Game 6: Cleveland vs Tampa Bay", sub_title "CLE vs TB (Oct 19)"), `exchange_index: 3`.
- Its milestone:
  - `type:"baseball_game"`
  - **`start_date:"2026-10-20T00:08:00Z"`**
  - `details.status:"scheduled"`, `details.league:"MLB"`
  - `details.home_team_id` / `away_team_id` (structured target IDs)
  - `details.main_game_event_ticker`, `details.playoff_game_number: 6`
  - `details.home_last_games` / `away_last_games`, `details.game_injuries`
  - `source_ids:{source_1_id,source_2_id,source_3_id}`
  - `notification_message:"The game is about to begin!"`
  - `primary_event_tickers` (GAME, SPREAD, TOTAL, EXTRAS), plus about 40 related tickers.
- Market values on the same event:
  - `occurrence_datetime` = `expected_expiration_time` = 2026-10-20T03:08:00Z (start + 3h)
  - `close_time` 2026-10-23T00:08:00Z
  - `custom_strike:{"baseball_team":"<structured_target_id>"}`, `strike_type:"structured"`
- **Does `start_date` give the scheduled start?** [INF, strongly supported] Yes. The ticker token `26OCT192008` reads as Oct 19, 20:08, and 20:08 ET (EDT, UTC−4) = 2026-10-20T00:08Z, exactly matching `start_date`.
  - The docs do not define `start_date` as first pitch. LC FAQ only says `expected_expiration_time` is "typically a few hours after the scheduled start" for sports.
- `GET /structured_targets` parameters: `ids` (repeatable, max 2000), `type`, `competition`, `page_size` (1-2000, default 100), `cursor`. Returns `{id, name, type, details, source_id, source_ids, last_updated_ts}`. [V:OAS]
- Live game data: `GET /live_data/milestone/{milestone_id}`, `/live_data/milestone/{id}/game_stats`, `/live_data/events/{event_ticker}`, `/live_data/batch`. [V:OAS]
  - [INF] These are public, since the OAS has no security block on them. Payload `details` is free-form JSON.

## 12. Exchange sharding (critical for the MLB live adapter) [V:SHARD][V:L1][V:L2]
- Matching is split across shards (`exchange_index`). **Sports with tags Tennis, Baseball, Basketball are on shard 3.** Combos are on 1; Crypto and Commodities on 2; everything else on 0.
  - Observed live: KXMLBGAME event, market and series all show `exchange_index: 3`.
- "Programmatic traders normally need to fund the relevant exchange shard before placing an order."
  - Use `POST /portfolio/intra_exchange_instance_transfer` or the UI.
  - `GET /portfolio/balance` returns `balance_breakdown` per shard.
  - Auto-rebalancing via a target allocation is optional and runs every 10 s.
- Routing rules:
  - `exchange_index >= 0` routes directly.
  - `-1`, or omitted with a ticker present, auto-routes. Auto-routing adds latency and bills every shard's Write bucket.
  - "An order ID alone cannot identify the exchange shard": cancels, decreases and amends need `market_ticker` (or `ticker`) or an explicit `exchange_index`.
- Subaccount balances are local to a shard. Order groups do not work across shards.

## Main gaps and conflicts (summary)
1. **Current fee schedule PDF (July 7, 2026 update) not retrieved** because of the Vercel 429. Verified formulas are from the Feb 5, 2026 version. How `fee_multiplier` (0.5 for KXMLBGAME) maps onto the 0.07 / 0.0175 coefficients is undocumented in fetched pages.
2. Fee rounding: the PDF says "round up to next cent"; the current docs (FR) describe ceil to 6 dp plus a rounding fee to $0.0001 (direct) or $0.01 (FCM), with per-order accumulator rebates. Treat FR as current. [INF]
3. Clock-skew tolerance for `KALSHI-ACCESS-TIMESTAMP`: not documented.
4. `client_order_id`: uniqueness scope, REST error `code` string and 409 body contents are not documented. There is no REST lookup by `client_order_id`.
5. No `seq` on `fill`, `user_orders` or `market_positions` WS messages, so there is no gap detection for private order and fill streams.
6. Candlestick max range per request (single market) and historical data start date: not documented.
7. Public, unauthenticated rate limits: not documented.
8. Legacy field removal dates conflict (May 14 vs May 28, 2026). Both have passed; fields remain in the spec.
