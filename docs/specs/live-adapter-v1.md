# Kalshi live exchange adapter specification v1 (roadmap F) — disabled by default

Status: implementation specification, 2026-10-19. Facts about Kalshi's API are in
`docs/specs/kalshi-api-facts-2026-10-18.md` (each tagged verified/unverified with source
URLs); everything this adapter relies on must cite that file, and every unverified fact
it depends on must be listed as an assumption in code and in the runbook.

## Non-negotiables

1. **Nothing in this PR may place a real order, call an authenticated endpoint, or read
   a credential during tests or demos.** Tests use a deterministic exchange double (an
   `httpx.MockTransport` implementing the documented V2 endpoints) and throwaway keys
   generated in the test.
2. **Disabled by default.** Every ordinary command path is paper or dry-run. A live
   gateway can only be constructed by `activate_live(...)` (below), which requires all
   gates at once; there is no automatic promotion from paper.
3. **Paper and live stay separate.** The live gateway writes only to a `live` ledger
   (the ledger enforces `environment`); the paper simulator only to `paper` ledgers.
4. **No blind retry of a write.** A create that times out or loses its connection is
   recorded `submit_unknown`; resolution goes through reconciliation by
   `client_order_id` (list orders filtered by ticker and time window, matched client
   side — there is no server filter). A resubmission, if ever, reuses the same
   `client_order_id` (documented: duplicate → 409 Conflict) and only after a
   reconciliation found no order; 409 always triggers lookup.
5. Startup reconciliation (open orders, recent orders, fills since cursor, positions,
   balance per shard) runs before any new submission; mismatches set `state_unknown`.

## Package `src/vader_intelligence/trading/kalshi/`

| Module | Responsibility |
|---|---|
| `auth.py` | `Signer` for RSA-PSS (SHA-256, salt = digest length) and Ed25519 keys via `cryptography`; signed string = `timestamp_ms + METHOD + path` where path starts at `/trade-api/v2` and excludes the query string; headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE` (base64). Clock: injectable; refuse to sign order creation if local clock skew vs. the exchange `Date` header of an authenticated, uncached response exceeds a configured bound (cancels and reads are always signed; revised after review) |
| `secrets.py` | `SecretProvider` protocol; `FileKeyProvider(path)` refuses files readable by group/other (mode must be 0600) and never logs content; `KeychainKeyProvider(service, account)` via macOS `security find-generic-password -w` (optional); providers return key material only to `auth.Signer`; `repr`/errors never include key bytes or paths' contents |
| `client.py` | HTTP client: base URL by environment (`production` = `https://external-api.kalshi.com/trade-api/v2`, `demo` = `https://external-api.demo.kalshi.co/trade-api/v2`), allowlist of the endpoints below only, timeouts, token-bucket rate limiter (read and write buckets, conservative defaults below the documented Basic tier), bounded exponential backoff for **reads only** on 429/5xx/transport errors, typed errors |
| `mapping.py` | Our `Order` (side yes/no, action buy/sell, quantity, limit price) ↔ V2 create body (`side` bid=buy YES / ask=sell YES, YES `price` dollars string, `count` string, `time_in_force`, `self_trade_prevention_type`, `client_order_id`, `post_only`, `reduce_only`, `exchange_index`). Buy NO at q ↔ ask (sell YES) at 1 − q; sell NO at q ↔ bid at 1 − q. Prices on the market's `price_ranges` grid; quantities on 0.01 steps (`count_fp`). Exchange order/fill objects → our `OrderStatus` / `Fill` (fees from `fee_cost` / `*_fees_dollars`) |
| `gateway.py` | `KalshiGateway(ExchangeGateway)`: `submit` (POST `/portfolio/events/orders`), `cancel` (DELETE `/portfolio/events/orders/{id}?market_ticker=`), `lookup(client_order_id)` (GET `/portfolio/orders?ticker=&min_ts=` paginated, client-side match; `None` only when the full window was scanned), `open_orders`, `fills_since(cursor)` (GET `/portfolio/fills`), `positions` (GET `/portfolio/positions`), `balance` (GET `/portfolio/balance`, shard breakdown; MLB is shard 3). Cancel reports `canceled` only once fills up to the cancel are retrievable (the executor releases reservations on `canceled`) |
| `dryrun.py` | `DryRunGateway`: same mapping and request construction (including signing with a throwaway key supplied by the caller, or unsigned), records each would-be request to the ledger/report, sends nothing, returns `submitted` results that never fill; used by the integrated dry-run path |
| `activation.py` | `LiveActivation` config (TOML) and `activate_live(config_path, *, acknowledge_hash, ledger, now)`: requires `enabled = true`; `environment` in {`demo`, `production`}; key id reference and secret provider config (no key material in the file); `RiskLimits` complete (`require_complete`); per-order and daily notional caps; allowed tickers/series list; the operator's `acknowledge_hash` equal to the SHA-256 of the config file; the ledger environment `live`; the kill switch disengaged by an explicit operator record. Any failure raises `ActivationRefused` with the list of failed gates. Returns a `KalshiGateway`. Nothing calls this except an explicit CLI command that also requires `--i-understand-this-places-real-orders` |
| `shutdown.py` | On kill switch or shutdown: stop new submissions, cancel open orders through the gateway (batch where available), reconcile, record outcome |

## Endpoints allowlisted (V2 and portfolio reads only)

POST `/portfolio/events/orders`; DELETE `/portfolio/events/orders/{order_id}`; GET
`/portfolio/orders`, `/portfolio/orders/{order_id}`, `/portfolio/fills`,
`/portfolio/positions`, `/portfolio/settlements` (added below), `/portfolio/balance`;
GET `/exchange/status`. No other path.

## Tests (`tests/trading/kalshi/`, no network)

Signing: verify signatures with the generated public key for RSA-PSS and Ed25519; path
excludes the query; timestamp in ms; skew refusal. Secrets: 0644 key file refused; key
bytes never in exceptions/repr/logs (capture logs). Mapping: all four side/action
combinations, price grid, fractional counts, round trip of exchange objects. Gateway
against the exchange double: accept, reject, partial fills, cancel race (fill after
cancel request), duplicate events, 429 with backoff on reads, **timeout on create →
submit_unknown → reconciliation finds the order (no duplicate) / finds none → safe
same-id resubmission → 409 → lookup**, restart with resting orders and fills since last
cursor, startup reconciliation mismatch → state_unknown blocks buys. Activation: every
gate individually refused; acknowledge hash mismatch; paper ledger refused; incomplete
limits refused. Dry run: requests constructed, nothing sent (transport asserts no call).
End-to-end through `Executor` with the double: intent → approval → order → fills →
positions reconcile → kill switch cancels open orders.

## Canary runbook (document only; never executed in this work)

`docs/live-canary-runbook.md`: prerequisites (review sign-offs, funded shard 3 on demo
first, credentials created by the user, key stored 0600 or in Keychain), readiness checks
(`vader live readiness` dry checks: config hash, limits complete, ledger live and empty or
reconciled, kill switch state, exchange status, clock skew), canary steps (demo first; one
minimum-size post-only order far from the touch; observe; cancel; reconcile), success
criteria, rollback (engage kill switch, cancel all, reconcile, disable config, archive
ledger), and the explicit statement that activation is a separate user decision.

## Revisions after the independent review of #25 (2026-10-19)

- **Clock skew.**
  - Only authenticated responses without an `Age` header update the observed offset.
    Public endpoints may be served from a cache (`cache-control: public`, facts L1).
  - A proven skew refuses order creation only. Cancels and reads are signed, so a
    shutdown can always cancel.
- **TLS.** The network transport uses httpx's bundled CA set with `trust_env=False`.
  `SSL_CERT_FILE`, `SSL_CERT_DIR` and proxies from the environment are ignored. This
  matters because the order body is not signed (facts A2).
- **Write outcomes.** Only 400, 401, 403 and 422 mean "not placed" (404 and 409 are
  handled separately). Any other non-2xx answer to a write leaves the outcome unknown
  and goes to reconciliation.
- **Caps and exits.**
  - Notional caps apply to buys only. A sell is still bounded by the venue's
    coverage check, so a position can always be exited.
  - The coverage check subtracts sell fills recorded since the last positions read
    (with a 60 s margin).
- **Startup.** `positions()` completes startup reconciliation only after the
  stray-order check passes.
- **Bindings.**
  - The gateway refuses buys once the ledger's current risk limits differ from those
    acknowledged at activation.
  - A live ledger is bound, at its first activation, to one Kalshi environment and key
    id (`ledger_binding` gate).
- **Ingest.**
  - A venue event resent later is idempotent: receipt time is not part of its
    identity.
  - Any ingest failure flags the state unknown.
  - A fill whose fee exceeds the order's fee bound is refused, so trading stops rather
    than reservations being silently underfunded.
  - A live approval must cite complete, recorded limits.

## Revision: venue settlements (2026-10-19)

Before this revision nothing recorded a live market's settlement: the venue paid out and
dropped the position, the ledger kept the holding, and reconciliation reported position
and balance mismatches indefinitely (safe, since buys stop, but never resolved).

- **Read.** `GET /portfolio/settlements` is allowlisted with `min_ts`, `limit` and
  `cursor` only. `KalshiGateway.settlements_since(cursor)` reads every page from a
  watermark (the fills cursor scheme and overlap) and returns `VenueSettlement`s
  (`trading/gateway.py`). Results `yes` and `no` settle at 1 and 0 per YES contract,
  `scalar` at `value` cents (ledger result `fair`). A record with any other result or a
  missing field, and two different records for one ticker (`conflicting_records`), come
  back as an `UnreadableSettlement` named by its ticker (or a digest), without hiding
  the other records. `source_ref` carries the parsed settlement instant, so another
  spelling of the same time is the same record.
- **Record.** `Executor.reconcile` reads settlements only after a clean fills
  reconciliation (otherwise `settlements_deferred`), and before positions and balance.
  A settlement `kalshi:<ticker>` is appended only when:
  1. the market has ledger orders (`settlement_foreign`);
  2. none of them is active (`settlement_orders_active`);
  3. for each of them the ledger holds at least the fill count the venue reported in
     its `reconciled` events (`settlement_fills_missing`): an order canceled at the venue
     (for example at close) becomes terminal before its fills are served;
  4. the fee is zero (`settlement_fee`);
  5. the settled YES/NO counts equal the ledger's holding and the revenue equals that
     holding's payout (`settlement_mismatch`).

  A market the ledger already settled (by the executor or an operator's correction)
  only has to agree on result and value (`settlement_conflict` otherwise); any other
  later change to the venue record surfaces only through the positions and balance
  checks. On any
  reason (also `settlement_unreadable`), nothing is recorded for that record, the
  others are still processed, and the cursor is kept so the next reconciliation reads
  the record again. Gateways without `settlements_since` (simulator, dry run) are
  unchanged.
- **Operator resolution.** A record that keeps a reason is resolved through the ledger,
  never by the adapter: append the settlement (or a correction superseding the current
  one) with the venue's result and value, after which the record agrees (a settlement
  fee is recorded as a cash adjustment); or, for a record that does not belong in this
  ledger or cannot be read, acknowledge it with `acknowledge_settlement(ledger, token,
  actor=..., note=..., at=...)` (an insert-once, auditable record; any recorded key
  counts; `token` is the name in the reason). For this ledger's own market an
  unreadable record needs both. Either lets the cursor move on. A `fill_conflict` (which
  defers settlements for every market) has no automated remedy.
- **Late fills.** A fill that arrives after its market's settlement was recorded stays
  refused by the ledger (`fill on a settled market` → `fill_conflict`). Under the rule
  above, every order on the market was terminal or filled with all venue-reported fills
  recorded and the holding equal to the venue's settled counts, so such a fill
  contradicts the venue's own final fill count or settlement. The refusal flags the
  state unknown for the operator. The ledger's rule is unchanged.
- **Assumptions.** Runbook §6 A21–A23 (record semantics, `min_ts` field, timing of the
  positions listing). All fail closed.
- **Pre-review (Opus, read-only) before Codex.** The first version recorded a
  settlement while fills were known to be missing when they netted to zero contracts,
  could not clear a conflict after an operator correction, treated another spelling of
  the settlement time as a different record, and let one unreadable record block every
  settlement. All four were confirmed by probes and are fixed as described above. A
  second read-only pass found no sequence that records a settlement with venue fills
  missing, and four minor gaps (a data-less acknowledgement ignored, two records for one
  ticker still blocking every market, and runbook remedies for fees, own unreadable
  records and `fill_conflict`), fixed as described above.
