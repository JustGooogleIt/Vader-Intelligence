# Live canary runbook (Kalshi adapter, roadmap F)

Status: DOCUMENT ONLY. Nothing in this runbook was executed while building the adapter:
no credential was created or read, no authenticated endpoint was called and no order was
placed. All adapter tests run offline against a synthetic exchange double.

**Activating live trading is a separate, explicit decision of the user.** Merging the
adapter, passing its tests or completing a dry run does not authorize it. Nothing in the
code base promotes paper to live: a live gateway exists only after
`trading.kalshi.activation.activate_live(...)` passes every gate, called from an operator
command that also requires `--i-understand-this-places-real-orders` (CLI wiring belongs
to the integrator).

Specification: `docs/specs/live-adapter-v1.md`. API facts and their verification status:
`docs/specs/kalshi-api-facts-2026-10-18.md` (cited as "facts §n").

## 1. Prerequisites

1. Review sign-offs recorded for the adapter (`src/vader_intelligence/trading/kalshi/`),
   its tests, this runbook, and the integrator's CLI wiring.
2. Demo first. Production only after a successful demo canary and a new, separate
   decision by the user.
3. Credentials are created by the user in the Kalshi UI (demo keys work only on demo,
   production keys only on production, facts §1). Ed25519 is recommended; RSA keys must
   be at least 2048 bits. Nobody else handles the private key.
4. The key is stored either
   - as a PEM file owned by the operator with mode `0600` (or `0400`), not a symlink:
     `chmod 600 /absolute/path/kalshi-demo.pem`; or
   - in the macOS Keychain as a generic password (`security add-generic-password -s
     <service> -a <account> -w` and paste the PEM when prompted).
   The activation file holds only the key id (a UUID) and the provider reference.
5. The account is dedicated to this system (primary subaccount, no manual trading, no
   other bot). Any foreign order or fill makes reconciliation fail closed.
6. The trading shard is funded: MLB markets are on exchange shard 3 (facts §12);
   "programmatic traders normally need to fund the relevant exchange shard before placing
   an order". On demo, fund shard 3 through the demo UI.
7. A fresh live ledger: `Ledger(path, "live")`, then
   - one `adjustment` cash event equal to the venue balance (`balance_dollars`);
   - the operator's complete `RiskLimits` (`ledger.record_limits(...)`; `require_complete`
     must pass) — note the returned limits hash for the config;
   - an explicit operator disengage of the kill switch (`ledger.set_kill_switch(False,
     reason, actor, at)`; a live ledger starts engaged).
8. Market metadata for the canary ticker re-checked from the public market endpoint:
   `price_ranges` (the source of truth for valid prices, facts §6) and the shard. For
   live markets use `Market.qty_step = 0.01`: Kalshi fills in 0.01-contract increments
   (facts §6), and a fractional fill on an order with a coarser step is refused by the
   ledger (fail closed, blocks risk-increasing orders).
9. Activation file, for example `live-demo.toml`:

   ```toml
   enabled = true
   environment = "demo"
   key_id = "<key id from the Kalshi UI>"
   risk_limits_hash = "<hash returned by ledger.record_limits>"
   max_order_notional = "1.00"
   max_daily_notional = "2.00"
   max_clock_skew_seconds = 5

   [secret]
   provider = "file"
   path = "/absolute/path/kalshi-demo.pem"

   [[markets]]
   series = "KXMLBGAME"
   exchange_index = 3
   tickers = ["<the one canary ticker>"]
   price_ranges = [{ start = "0.0000", end = "1.0000", step = "0.0100" }]
   ```

   The operator acknowledges the exact bytes: `shasum -a 256 live-demo.toml`. Any edit
   changes the hash and activation refuses until it is acknowledged again.

## 2. Readiness checks (`vader live readiness`, dry: no credential, no order)

The integrator's readiness command should report, without loading the key:

| Check | Source | Pass |
|---|---|---|
| Config hash | `activation.check_gates(...)` gate `acknowledge_hash` | operator hash = SHA-256 of the file |
| All local gates | `check_gates` results (every gate except `secret_loads`) | all pass |
| Limits complete | gates `risk_limits_complete`, `risk_limits_hash` | pass |
| Ledger | gates `ledger_live`, `ledger_consistent`; ledger empty or last reconciliation clean | pass |
| Kill switch | gate `kill_switch_disengaged` (explicit operator record, not in the future) | pass |
| Exchange status | public GET `/exchange/status` (`KalshiGateway.exchange_status()`) | `exchange_active`, `trading_active` and shard 3 `trading_active` true |
| Clock skew | `Date` header of that response vs. the local clock (`Signer.observed_offset_ms`) | within `max_clock_skew_seconds` |

Also check that the window is outside the weekly maintenance pause (Thursday 3:00-5:00 AM
ET, facts §9) and before the canary market's close time.

## 3. Canary steps (demo first)

1. `activate_live(config, acknowledge_hash=..., ledger=..., now=...)`; on
   `ActivationRefused` read `error.failed` (every failed gate) and stop.
2. Startup reconciliation before any order: `executor.reconcile(now)` must be clean
   (open orders, fills since the cursor, positions incl. resting orders, balance). The
   gateway refuses every submission until positions and balance were read
   (`local:startup_reconciliation_pending`).
3. Place ONE minimum-size, post-only, good-till-canceled buy far from the touch (for
   example 1.00 contract at least 10 ticks below the best YES bid), through the normal
   intent -> risk -> executor path. It must not fill.
4. Observe: the order is `open` in the ledger, appears once in GET `/portfolio/orders`
   (resting) with the same `client_order_id`, price and count; reconciliation stays clean.
5. Cancel through the executor. Expect `canceled` (or `pending` followed by a clean
   reconciliation that records the terminal state).
6. Reconcile again: clean; no active orders; balance unchanged; `ledger.verify()` empty.
7. Run the shutdown routine once (`shutdown(executor, now=..., reason="canary end",
   actor=...)`): it halts submissions, engages the kill switch, cancels open orders and
   strays, reconciles and records its summary. `report.clean` must be true.
8. Optional, only with explicit user approval: a single fill test at minimum size to
   verify fee and balance reconciliation (assumption A13 below). Compare the fill's
   `fee_cost`, the ledger cash and `balance_dollars`.

## 4. Success criteria

- Every gate passed; nothing was refused unexpectedly.
- Exactly one order was created at the venue for the canary `client_order_id`.
- No `submit_unknown`, no `lookup_failed`, no `positions_unavailable`, no mismatches.
- Cancel confirmed; reconciliation clean before and after; `ledger.verify()` empty.
- Shutdown report clean; kill switch engaged at the end.
- No 401 (signature) or clock-skew refusal; no 429 on writes.

## 5. Rollback

1. Engage the kill switch and run `shutdown(...)` (halts the gateway, cancels open
   orders and strays one by one — the batch-cancel endpoint is not allowlisted — and
   reconciles). If the process is gone, cancel in the Kalshi UI.
2. Reconcile until clean; investigate every unresolved reason before anything else.
3. Disable the config (`enabled = false`; the hash changes, so activation refuses).
4. Archive the ledger (`ledger.backup(path)` verifies the copy); keep it read-only.
5. Revoke the API key in the Kalshi UI if there is any doubt about its custody.

## 6. Assumptions relying on UNVERIFIED or undocumented facts

Each is also listed in the docstring of the module that relies on it. Verify on demo
before production.

| # | Assumption | Facts | Module | If wrong |
|---|---|---|---|---|
| A1 | Clock-skew tolerance undocumented; we refuse to sign when the `Date` header proves skew > 5 s | §1 gap | `auth` | 401s; tighten/loosen the bound |
| A2 | The request body is not part of the signed text | §1 INF | `auth`, `dryrun` | 401 on every write (fail closed) |
| A3 | `min_ts` on order and fill listings is Unix seconds on creation (or a later) time | §2.4/§2.5 | `gateway` | lookups/fills miss records: raise or flag mismatches |
| A4 | REST reads lag by a short delay: "not found" only 30 s after creation; fills re-read with 1 h overlap | §9 | `gateway` | a lagging order is marked not found; caught later as a stray (blocks buys) |
| A5 | Canceled/executed orders older than the historical cutoff are only on `/historical/orders` (not allowlisted): no "not found" for orders older than 3 days | §2.4 V:HIST, INF | `gateway` | manual reconciliation of old orders |
| A6 | An empty or absent `cursor` ends a listing | INF | `gateway` | pagination error (fails closed) |
| A7 | Order ids are hexadecimal UUID-like strings (allowlisted path pattern) | UNV | `client` | cancels/get-order refused (fail closed) |
| A8 | The create response is a top-level object with `order_id` | §2.1 (nesting UNV) | `gateway` | submit recorded unknown, then reconciled |
| A9 | A 429 or 5xx on a write may have been processed: treated as unknown | §5, §10 gap | `client` | extra reconciliation only |
| A10 | 400/401/403 on create mean "not placed" | INF | `client`, `gateway` | an order the ledger calls rejected surfaces as a stray (blocks buys) |
| A11 | Buy NO at q = `ask` at 1 − q; sell NO at q = `bid` at 1 − q | §2.1 INF, V:DIR | `mapping` | wrong-side orders: verify on demo with a far-from-touch order |
| A12 | A REST fill's `book_side` is that of our order | UNV | `mapping` | fills refused as mismatches (fail closed) |
| A13 | `fee_cost` is the fill's whole fee, nonnegative, and fills reconcile with `balance_dollars` (rounding fees, §7 FR) | UNV | `mapping` | `balance_mismatch` after fills (fail closed) |
| A14 | `balance_breakdown[].balance` is integer cents (text = dollars) | UNV | `mapping` | shard figures wrong (informational only) |
| A15 | The balance endpoint also draws read tokens | §5 | `client` | none (conservative) |
| A16 | `security find-generic-password -w` prints text, or hex for secrets with newlines | UNV | `secrets` | key cannot be parsed (activation refused) |
| A17 | A ticker's series is the text before its first `-` | INF | `gateway` | ticker refused as not allowed |
| A18 | The account and primary subaccount are dedicated to this ledger | §2.4 | `gateway` | foreign activity blocks risk-increasing orders |
| A19 | `self_trade_prevention_type = taker_at_cross`; `reduce_only` only on IOC sells | §2.1 V:OAS | `mapping` | exchange rejects the order (definitive) |
| A20 | `fill_count_fp`/`remaining_count_fp`/`initial_count_fp`, `yes_price_dollars`, `book_side` and `status` on Order objects as in the OAS; `outcome_side` of V2 orders unused | §2.4 | `mapping` | lookups raise (fail closed) |

## 7. Known limitations

- No WebSocket: fills and order states come from REST polling during reconciliation,
  so they lag by the REST delay; there is no gap detection for private streams anyway
  (facts §4).
- No amend, decrease, batch create or batch cancel (not allowlisted); cancels are one by
  one. No `/historical/*` reads: very old unresolved orders need manual reconciliation.
- A cancel that races a fill reports `pending` until the next reconciliation records the
  fills; the reservation is held meanwhile (conservative).
- Resubmission with the same `client_order_id` is available at the gateway (it turns into
  409 -> lookup when the order exists) but the executor never resubmits: an order whose
  outcome is unknown is resolved only by reconciliation.
- Fee schedules in `trading/fees.py` remain unverified (facts §7); the live adapter uses
  the exchange's reported fees for fills, while risk sizing still uses the schedule.
- The daily notional cap counts gross notional (quantity x limit) of all orders created
  that UTC day except those definitively never placed.
