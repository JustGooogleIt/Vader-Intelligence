# Historical paper backtest and live-adapter dry run v1

Status: implemented 2026-10-19 (roadmap D/H). Both paths are simulations: neither sends
a request to an exchange nor touches a live ledger.

## 1. Historical paper backtest (`research/backtest.py`, `vader backtest run`)

Inputs:

- a registry dataset and a registered model trained on that dataset and split (refused
  otherwise);
- the history archive (opened read-only): the selected markets' listing records and
  one-minute candles from the feature stage (`hist-candles`, windows ending at C) merged
  with the execution-only stage (`hist-candles-post`, windows after C).

Construction:

| Element | Source | Rule |
|---|---|---|
| Forecast | the row's features (computed from candles ending at C) | one per row, `computed_at = C`, probability from the registered artifact |
| Execution snapshots | candle closing best bid/ask within [C − 120 s, C + 300 s] | price-only (`depth_known = False`); crossed or empty candles skipped |
| Market | dataset row + listing record | status open until settlement; close time from the listing |
| Settlement | archived `result` and `settlement_ts` | rows without settlement evidence are listed under `skipped` |

The trading simulator then applies its conservative defaults: decisions see only
snapshots at or before C, an order arrives after the modelled latency, the book at
arrival is the worst of the bracketing snapshots, and a gap above 120 s makes execution
unidentifiable. **Candles carry no sizes**, so with the default configuration nothing
fills; fills appear only under the labelled `assumed_depth` scenario (contracts assumed
at the best level, then the 50% depth haircut). Without `--ledger` the CLI runs a
scenario grid: both provisional fee schedules × slippage {0, 1} tick × latency {250 ms,
1250 ms} × maker model `none` × assumed depth {none, each `--assumed-depth`}.

Segments: `development` (in-sample), `validation` (selection-biased: it compares
models) and `holdout`. A holdout run requires `--acknowledge-holdout` and is recorded in
the registry's holdout ledger (the first use consumes it; later uses are reuses and are
never reported as untouched).

What a backtest is not: candle closes are one-minute summaries, not executable books;
historical retrieval happened after the fact; nothing here is evidence of edge or of
live profitability.

## 2. Live-adapter dry run (`trading/shadow_dryrun.py`, `vader live dry-run`)

For every contract-4 decision in an evidence archive with a probability from the named
registered forecaster:

1. the latest archived order book at or before the decision's cutoff (`kind = book`
   observations; retrieval time as `as_of`);
2. the declarative edge-threshold strategy (`simulator.propose`);
3. the risk engine (paper environment, sizes clipped to the limits by default) and the
   executor (`submit_requested` is durable before the gateway call);
4. a `DryRunGateway` for `demo` or `production` URLs: it builds the exact request with
   the live adapter's market rules and mapping and, with `--sign-with-throwaway-key`,
   signs it with an in-memory Ed25519 key generated for the run. It has no HTTP client.

The output lists every decision's outcome (no intent with its reason, a risk refusal
with its reasons, or `submitted_dry`), the would-be requests (signatures replaced by
their SHA-256) and the ledger verification. The ledger is a paper ledger, so a dry run
can never reach a live ledger. A dry run shows that a forward-shadow forecast can become
a well-formed order request under every local check; it does not show that the exchange
would accept it, that it would fill, or any profit.
