# Execution, risk and sizing specification v1

Status: implementation specification for roadmap items D1, D2 and E (2026-10-18).
Scope: one authoritative order/fill ledger, one risk layer shared by paper and future live
execution, fractional Kelly sizing for $1 binary contracts, and an event-driven paper
simulator. The live exchange adapter (item F) implements the `ExchangeGateway` protocol
below in a later PR; nothing here can reach a network.

## 1. Principles

1. Money is `decimal.Decimal` everywhere. Prices are dollars per contract quantized to
   the market tick (Kalshi `linear_cent` markets: 0.01). Quantities are `Decimal`
   contract counts quantized to the market's quantity step (default 1; fractional
   `_fp` sizes allowed when a market declares a finer step). Floats never touch money.
2. Strategy intent, risk approval, order, exchange event and fill are distinct records.
   A strategy can only *propose*; only the risk engine creates approvals; only an
   approval can become an order; only a gateway event can change an order.
3. Paper and live are different ledgers by construction. A ledger's `environment`
   (`paper` | `live`) is written once at creation and is immutable (trigger-enforced).
   A paper ledger rejects exchange-sourced fills; a live ledger rejects simulated
   fills. There is no API that copies or promotes records between ledgers.
4. Append-only history. Event tables have no UPDATE/DELETE (triggers). Current state
   (orders, positions, cash, reservations) is a deterministic projection of events;
   any cached snapshot must be re-derivable and is verified on open.
5. Unknown is not zero. Any required state that is unknown (unreconciled order,
   missing balance, stale data, unknown model) blocks new risk-increasing orders.
6. Every module is deterministic given its inputs, configuration and seed.

## 2. Package layout

`src/vader_intelligence/trading/`:

| Module | Responsibility |
|---|---|
| `money.py` | Decimal parsing/quantization, tick and step grids, rounding helpers (fees round up to the cent, payouts exact) |
| `instruments.py` | `Market` (ticker, event_ticker, tick, qty_step, min/max price, status, close time, settlement) and correlation-group keys |
| `ledger.py` | SQLite ledger: schema, migrations, append-only triggers, environment guard, idempotent appends, projections |
| `orders.py` | Order state machine and event application (idempotent, duplicate/out-of-order tolerant) |
| `portfolio.py` | Positions (YES/NO holdings with Kalshi-style netting), cash, reservations, realized/unrealized PnL, settlement and corrections |
| `fees.py` | Versioned fee schedules (data + provenance + assumptions); fee computation per fill |
| `risk.py` | `RiskLimits`, `RiskEngine.evaluate(intent, context) -> Approval | Rejection`, kill switch |
| `kelly.py` | Fractional Kelly sizing for binary contracts with costs, uncertainty, existing exposure and group caps |
| `gateway.py` | `ExchangeGateway` protocol and the event types a gateway emits |
| `simulator.py` | `PaperExchange` (implements `ExchangeGateway`) and the event loop `run_paper(...)` |
| `marketdata.py` | Book/quote snapshot types; readers that turn archive observations into time-ordered snapshots |
| `report.py` | Paper-run report (machine JSON and concise text) and the sensitivity grid |

## 3. Records

All identifiers are strings; all timestamps aware UTC ISO-8601; all amounts Decimal
strings in storage (`format(d, 'f')`).

- `OrderIntent` (strategy output; immutable): `intent_id` (deterministic digest of its
  content), `strategy_id`, `forecast_ref` (opaque dict: model/forecaster id, artifact
  hash, decision or forecast record id, probability string, computed_at),
  `ticker`, `event_ticker`, `side` (`yes`|`no`), `action` (`buy`|`sell`),
  `limit_price`, `max_quantity` (optional; Kelly decides if absent), `time_in_force`
  (`ioc`|`gtc`), `post_only` (bool), `created_at`, `rationale` (short string).
- `Approval`: `approval_id`, `intent_id`, approved `quantity` (>0), `limit_price`,
  `limits_version` (hash of the `RiskLimits` used), `state_digest` (hash of the
  projection used), `checks` (ordered list of `{check, value, limit, passed}`),
  `sizing` (Kelly inputs/outputs), `created_at`.
- `Rejection`: `intent_id`, ordered `reasons` (stable string codes, see §6), the same
  `checks` list, `created_at`. Rejections are recorded too (audit trail).
- `Order`: `client_order_id` (durable, generated before any submission:
  `vi-{environment}-{approval_id[:16]}-{n}`), `approval_id`, ticker, side, action,
  quantity, limit_price, time_in_force, post_only, `created_at`. One approval yields at
  most one order (UNIQUE).
- `OrderEvent` (append-only): `event_id` (gateway-unique; for simulated events a
  deterministic digest), `client_order_id`, `exchange_order_id` (nullable), `kind`
  (`submit_requested`, `submitted`, `accepted`, `rejected`, `fill`, `cancel_requested`,
  `canceled`, `expired`, `amended`, `submit_unknown`, `reconciled`), `sequence`
  (gateway sequence if any), `exchange_ts`, `received_at`, `payload` (dict).
- `Fill` (append-only, UNIQUE `fill_id`): `fill_id`, `client_order_id`, ticker, side,
  action, `price`, `quantity`, `fee`, `liquidity` (`maker`|`taker`), `exchange_ts`,
  `source` (`simulated`|`exchange`).
- `Settlement` (append-only): `settlement_id`, ticker, `result` (`yes`|`no`|`fair`),
  `settlement_value` (YES payout per contract, 0..1), `source_ref`, `effective_at`,
  `supersedes_id` (corrections append a new row that supersedes; never update).
- `CashEvent` (paper only for deposits; live balance snapshots are `balance_observed`
  events): `kind` (`deposit`|`withdrawal`|`adjustment`|`balance_observed`), amount.
- `KillSwitch` (append-only): `engaged` (bool), `reason`, `actor`, `at`. Current state =
  latest row; default when no row exists: **engaged** for live, disengaged for paper.

## 4. Ledger (SQLite)

- Own database file, separate from the evidence archive and from the research registry.
  WAL, `synchronous=FULL`, `foreign_keys=ON`, a process `flock` on `<db>.lockfile` for
  writers (same pattern as `storage.writer_lock`). Every append is one short
  `BEGIN IMMEDIATE` transaction; no network call inside a transaction.
- `ledger_meta(key PRIMARY KEY, value)` holds `environment`, `schema_version`,
  `created_at`; trigger forbids UPDATE/DELETE. Opening a ledger states the expected
  environment; mismatch raises.
- Tables: `intents`, `approvals`, `rejections`, `orders`, `order_events`, `fills`,
  `settlements`, `cash_events`, `kill_switch`, `risk_limit_versions` (operator-written
  limit snapshots with hash), `processed_inputs` (idempotency/checkpoint keys for the
  simulator and reconciliation). All but `processed_inputs` are append-only.
- Idempotency: inserting an existing primary/unique key with identical content returns
  the existing row; different content raises `LedgerConflict` (no overwrite).
- Environment guard triggers: in a `paper` ledger `fills.source` must be `simulated`;
  in a `live` ledger it must be `exchange`. Same for `order_events` payload `source`.
- `verify()` recomputes every projection from events and checks invariants (§7);
  `backup(path)` uses the SQLite online backup API then verifies the copy.

## 5. Order state machine (orders.py)

States: `new` (persisted, not yet sent) → `pending_submit` (submit requested) →
`open` (accepted, quantity remaining) → `filled` | `canceled` | `expired` | `rejected`;
plus `unknown` (submission outcome not known: timeout, connection loss).

Rules:
1. Events are applied idempotently by `event_id`; fills by `fill_id`.
2. Cumulative filled quantity is the sum of distinct fills; it never decreases and never
   exceeds the order quantity (excess → `LedgerConflict`, state flagged `inconsistent`).
3. A fill may arrive after `cancel_requested` or even after `canceled` (cancel race);
   it is accepted, and the order's final state reflects remaining quantity.
4. Out-of-order events: if a gateway sequence is present, the projection orders by it;
   otherwise by `(exchange_ts, received_at, event_id)`. Status regressions from stale
   events (e.g. `accepted` after `filled`) never reopen a terminal order.
5. `unknown` blocks a resubmission of the same order. Resolution only through a
   `reconciled` event produced by querying the venue (paper: the simulator's own book;
   live: order lookup by client order id). A timed-out submit is never blindly retried.
6. Remaining quantity of an `open` order is reserved (cash for buys at the limit price
   plus maximum fee; contracts for sells).

## 6. Risk engine (risk.py)

`RiskLimits` fields (all `Optional[Decimal|int]`; `None` = unset):
`max_order_contracts`, `max_order_cost`, `max_market_exposure`, `max_event_exposure`,
`max_group_exposure`, `max_portfolio_exposure`, `min_cash_buffer`, `daily_loss_limit`,
`max_drawdown`, `max_data_age_seconds`, `max_forecast_age_seconds`,
`max_orders_per_minute`, `max_open_orders`, `max_price_deviation` (limit vs reference
mid), `min_edge` (probability minus all-in cost), `kelly_fraction`,
`kelly_portfolio_cap` (sum of target fractions across open groups),
`auto_kill_on_loss_stop` (bool).

- `SIMULATION_DEFAULTS`: a conservative, clearly labelled instance for paper runs (e.g.
  kelly_fraction 0.10, portfolio cap 0.25 of bankroll, 5% of bankroll per event, data age
  ≤ 300 s, ≤ 10 orders/min, ≤ 20 open orders, daily loss 5%, drawdown 15%). Values are
  documented as simulation assumptions, not recommendations.
- Live: no defaults. `RiskLimits.require_complete()` raises when any field is unset; the
  live path calls it, so an unconfigured live deployment rejects every order.
- Exposure = maximum loss: a YES holding of n at average cost p risks n·p; a NO holding
  risks n·q; open buy orders add their reserved cost. Event exposure aggregates both
  contracts of one event (they are mutually exclusive outcomes of one game, so a YES on
  team A and a YES on team B are a hedge only up to the paired quantity; use the
  conservative sum of max losses unless the pair is netted by the venue).

`evaluate(intent, ctx)`; `ctx` provides: projection (positions, cash, reservations, open
orders, recent order times, equity history), market snapshot (with `as_of`), forecast
status, clock `now`, limits, kill-switch state, fee schedule, `unknown_state` flags.
Checks in this fixed order; all failing reasons are collected (not only the first):

1. `kill_switch_engaged` (risk-increasing intents only; reduce-only sells pass to 2+)
2. `limits_unset` (live), `state_unknown` (any unknown flag, unresolved `unknown`
   order, unreconciled ledger), `market_unknown`, `market_not_open`
3. `data_stale` (now − snapshot.as_of > max_data_age), `forecast_stale`,
   `forecast_unavailable` (missing/incompatible model → never a fallback)
4. `price_off_grid`, `price_out_of_bounds`, `price_deviation`
5. `edge_below_minimum` (calibrated probability − limit price − per-contract fee)
6. sizing (Kelly, §8) → `size_zero` when the target adds nothing
7. `order_contracts_limit`, `order_cost_limit` (clip down to the limit instead of
   rejecting when `clip_to_limits=True`, recording the clip)
8. `market_exposure_limit`, `event_exposure_limit`, `group_exposure_limit`,
   `portfolio_exposure_limit` (same clipping rule)
9. `insufficient_buying_power` (cost + max fee + reservations + `min_cash_buffer` >
   cash)
10. `daily_loss_stop`, `drawdown_stop` (and engage the kill switch if
    `auto_kill_on_loss_stop`)
11. `order_rate_limit`, `open_orders_limit`

Strategies and research pods receive a read-only risk interface (`evaluate` only via the
executor). There is no function that edits limits except the operator CLI path that
appends a `risk_limit_versions` row.

## 7. Portfolio accounting (portfolio.py)

Holdings per ticker: `yes_qty`, `yes_cost`, `no_qty`, `no_cost` (cost includes fees
allocated to the opening fills). Kalshi-style netting: when a buy of the opposite side
would create simultaneous YES and NO holdings, `m = min(yes_qty, no_qty)` pairs are
netted immediately: cash += m × $1; each side's quantity and cost basis reduced
proportionally (average cost), the difference recorded as realized PnL. A `sell` of a
side reduces that side; selling more than held is rejected (no naked short through
`sell`; shorting YES is done by buying NO).

Cash: buy → cash −= price·qty + fee; sell → cash += price·qty − fee; netting → +$1 per
pair; settlement → YES holders receive `settlement_value`·qty, NO holders
`(1 − settlement_value)`·qty, holdings closed, realized PnL recorded. A settlement
correction reverses the superseded settlement's effects and applies the new one (both
recorded as events; the projection is recomputed from scratch).

Unrealized PnL: conservative mark = liquidation value at the best bid of the held side
(YES held → best YES bid; NO held → best NO bid); mid mark reported separately.
Equity = cash + Σ conservative marks. Invariants checked by `verify()`:
- cash = deposits − withdrawals + Σ sale proceeds − Σ purchase costs − Σ fees +
  Σ netting credits + Σ settlement payouts (exact Decimal)
- realized + unrealized + fees reconcile to equity − net deposits
- no negative holdings, no negative cash in paper, filled ≤ ordered for every order
- reservations = Σ remaining open-order requirements (never negative, released on
  terminal states)

## 8. Fractional Kelly (kelly.py)

For buying one side at limit price `p` with all-in per-contract cost
`c = p + fee_per_contract(p)` and model probability `q` for that side winning:

- Full-Kelly bankroll fraction `f* = (q − c) / (1 − c)` if `q > c`, else 0.
- Uncertainty: use `q_adj = q − haircut` where `haircut` is a configured probability
  margin and/or a supplied model standard error times `z`; sizing uses `q_adj`.
- Fractional: `f = kelly_fraction × f*(q_adj)`.
- Existing exposure: target position value `f × bankroll`; order adds only
  `target − current same-side exposure` (never negative; never closes an opposite
  position implicitly).
- Correlation groups: both contracts of one event form one group; only one direction per
  group is sized (an intent on the opposite side of an existing group position is
  treated as reducing first). Across simultaneously open groups the sum of target
  fractions is capped by `kelly_portfolio_cap`; when exceeded, every new target is scaled
  down proportionally (and existing positions are never auto-increased to "use" the cap).
  Groups whose dependence the model cannot support (configurable predicate, default: same
  team on the same date) are capped jointly.
- Contracts `n = floor_to_step(f × bankroll / c)`; zero when below one step.
- Bankroll = conservative equity minus reservations (never including unrealized gains
  above the conservative mark).

Hand-verifiable cases (must be tests): q=0.60, p=0.50, fee=0 → f*=0.20; with
kelly_fraction 0.25 and bankroll 1000 → f=0.05, cost per contract 0.50 → 100 contracts.
q=0.50, p=0.50 → 0. q=0.55, p=0.50, fee=0.02 (c=0.52) → f* = 0.03/0.48 = 0.0625.
Properties: monotone non-decreasing in q, non-increasing in p and fee, zero when
q ≤ c, never exceeds the cap, proportional scaling preserves ordering.

## 9. Fees (fees.py)

`FeeSchedule(version, source_url, retrieved_at, verified: bool, assumptions: list,
taker_rate, maker_rate, rounding)` and `fee(price, quantity, liquidity)`. The Kalshi
general schedule (to be confirmed from the official fee page; record verification
status): taker fee = `ceil_to_cent(taker_rate × qty × p × (1 − p))`, maker fee likewise
with `maker_rate` (0 when the market charges no maker fee). The schedule actually used
is stored in every run manifest. Unverified parameters are flagged in reports.

## 10. Gateway protocol (gateway.py)

```python
class ExchangeGateway(Protocol):
    environment: str                       # "paper" | "live"
    def submit(self, order: Order) -> SubmitResult       # accepted | rejected | unknown
    def cancel(self, client_order_id: str) -> CancelResult
    def lookup(self, client_order_id: str) -> OrderStatus | None   # reconciliation
    def open_orders(self) -> list[OrderStatus]
    def fills_since(self, cursor: str | None) -> tuple[list[Fill], str | None]
    def positions(self) -> dict[str, Holding]
    def balance(self) -> Decimal | None
```

The executor (`trading/executor.py`) is the only caller: it persists `submit_requested`
before calling `submit`, records the result (or `submit_unknown` on timeout/exception),
and runs `reconcile()` (lookup by client id, open orders, fills, positions, balance)
before any new submission after a restart or an unknown result. Reconciliation
mismatches set `state_unknown` until resolved.

## 11. Paper simulator (simulator.py)

Event loop over a merged, time-ordered stream: market snapshots (books and/or listing
quotes from the archive or synthetic), forecasts, settlements, and the simulator's own
scheduled order arrivals. Deterministic: ties ordered by `(ts, kind_rank, seq)`; RNG
seeded per run.

- Latency: `decision_latency` + `submit_latency` (fixed or seeded uniform range). An
  order reaches the matching engine at `arrive = decided_at + latency`.
- Book state at arrival (`book_state_rule`): `prior_snapshot` (latest snapshot ≤
  arrive) or **`worst_of_bracketing`** (default: worse price/smaller depth of the latest
  snapshot ≤ arrive and the first ≥ arrive). If the bracketing gap exceeds
  `max_snapshot_gap`, the order result is `execution_unidentifiable` (recorded, no fill).
- Taker matching: walk the opposite-side bid ladder (YES ask levels are 1 − NO bids)
  up to the limit price, consuming `depth_haircut × displayed quantity` per level
  (default haircut 0.5), plus `extra_slippage_ticks`. Partial fills allowed; remainder
  canceled for IOC, rests for GTC.
- Maker (resting) fills (`maker_fill_model`): **`none`** (default; resting orders never
  fill), `trade_through` (fill only when a later snapshot's opposite best bid implies a
  price strictly better than our limit for the counterparty, quantity capped by our
  remaining size and by the haircut depth at that level), `optimistic_touch` (labelled
  upper bound: fill at touch). Touching our price is never proof of a maker fill.
- Fees per fill from the run's `FeeSchedule`.
- Settlement events pay out per §7; markets without settlement evidence at run end are
  reported as open with conservative marks.
- Restart: each processed input key is recorded in `processed_inputs` in the same
  transaction as its effects; a restarted run skips processed keys and produces an
  identical ledger (test: crash injected between every pair of durable steps).

`run_paper(config, inputs, ledger_path) -> PaperRunReport`; config includes strategy
(declarative: thresholds on edge, side selection, Kelly), risk limits, fee schedule,
latency, fill model, book rule, haircut, seed. The report contains the run manifest
(input digests, config digest, fee version, assumption list), orders, fills, PnL (cash,
realized, conservative and mid unrealized), exposure history, rejections by reason, and
`unidentifiable` counts. `sensitivity(config, grid)` reruns over fee × slippage ×
latency × fill-model scenarios into disposable ledgers and tabulates results.

## 12. Tests required

Hand-checked Kelly and fee cases; property tests (hypothesis) for Kelly monotonicity,
accounting invariants under random fill/cancel/settle sequences, idempotent duplicate
events, out-of-order delivery; crash injection between durable steps (simulator and
executor); environment guard (paper ledger refuses exchange fills and vice versa);
kill-switch persistence across reopen; unknown submission blocks resubmission until
reconciliation; reservations released on every terminal path; cancel/fill race;
settlement correction; restart produces identical projections; sensitivity report
reproducibility (same seed → identical JSON).
