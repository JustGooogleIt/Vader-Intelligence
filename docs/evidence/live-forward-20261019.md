# Live forward shadows, 2026-10-19

The data is **live and forward**: public read-only Kalshi and MLB endpoints, two
isolated shadow archives under the test label, no launchd and no production path. No
order was sent and no credential exists. It covers one game and one decision per
shadow. That is not evidence of edge, and a single forecast says nothing about a
model's quality.

The machine-readable extract is `docs/evidence/live-forward-20261019.json`. It was
produced by a read-only script that opens both archives with `mode=ro`.

## Setup

- **Game:** `KXMLBGAME-26OCT192008CLETB` (MLB game 849815), postseason, scheduled
  start 2026-10-20T00:08:00Z.
  - Protocol `mlb-t60-v1`, so C = 2026-10-19T23:08:00Z.
  - Selected contract: `KXMLBGAME-26OCT192008CLETB-CLE` (the away team); its
    alternative is `-TB`.
- **Rule-v2 shadow** (`/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-rule-v2`):
  - code `622caadd` (#18 head), eligibility rule v2, baselines only;
  - decision contract 3;
  - cycles on a fixed 120 s grid from 22:30:10Z.
- **Model shadow** (`/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-models`):
  - code `ab43c48` (in #28, before the #31 and #33 repairs);
  - decision contract 4, with three registered forecasters, all trained by research
    pods on the 2026 history dataset;
  - the forecasters are logistic `d316e849`, gradient boosting `a6d69da1` and symbolic
    `06891d03`;
  - **all three were rejected by the protected evaluator**, since none beats the
    market midpoint on validation;
  - cycles on a fixed 120 s grid from 21:56:10Z.
- Both shadows: cadence 120, collector deadline 90, grace 5, tick budget 15. One
  bounded `shadow-evaluate` runs after the game, at about 05:15Z on 2026-10-20.
  Every cycle up to C exited 0, and collection was `complete` in each.

## Decisions at C (both created at 23:08:12Z, publication `allowed`)

| Forecaster | Rule-v2 shadow (contract 3) | Model shadow (contract 4) |
|---|---|---|
| constant-v1 | 0.5 | 0.5 |
| midpoint-v1 | abstained: `book_availability_after_cutoff` | abstained: `book_availability_after_cutoff` |
| logistic `model:d316e849de67b8cd` | — | **0.461393** |
| gradient boosting `model:a6d69da1f90519ee` | — | **0.438094** |
| symbolic `model:06891d035c2156ad` | — | **0.413415** |

The market at C, from the models' own inputs, used listing quotes retrieved by 23:04:15
and witnessed by 23:06:11:
- mid 0.415, spread 0.01 (bid 0.41, ask 0.42);
- pair mid 0.415, overround 0.01;
- no mid change over 15 or 45 minutes;
- `last_trade_gap_c` 0.005.

The symbolic model is a linear map of the pair midpoint, so it stays near the market.
The logistic model is 4.6 points above it.

## The midpoint abstention (D-016)

| Book for the CLE contract | Retrieved | First witnessed |
|---|---|---|
| models shadow | 23:04:15.6 | 23:06:11.9 (≤ C) |
| models shadow | **23:06:15.8** | **23:08:12.2 (after C)** |
| rule-v2 shadow | 23:06:15.7 | 23:08:12.2 (after C) |

Each cycle runs the tick first, which witnesses earlier evidence, and then collects.
The latest book retrieved by C was therefore witnessed only by the next tick, after C.
Book selection never falls back to an older book, so the midpoint abstained in both
shadows. This follows from the specified rules rather than from a fault, and it
affects almost every phase of the cycle grid. `docs/roadmap/decisions.md` D-016 gives
the analysis and the options for the user.

**Point in time.** No forecaster used evidence witnessed after C. The models' 20
selected quote fetches were all witnessed. The latest was retrieved at 23:04:15 and
witnessed at 23:06:11. The 23:06 listing fetches were witnessed after C and were not
used.

## D-015 (`last_trade_gap_c`)

Forward value: 0.005, present rather than `None`. On 2026 history the feature is
never `None`, with |value| median 0.005 and p99 0.01. On 2025 history it is `None` in
6.4% of rows. This one forward value lies inside the history distribution, but a
single row cannot show whether the two definitions agree, so D-015 stays open.

## Live-adapter dry run on the real forward decision

Command (code: the #35 head `7686513`; the dry-run path is unchanged since #33):

```sh
vader live dry-run --archive <model shadow>/shadow.sqlite3 --forecaster model:<id> \
  --ledger <new paper ledger> --sign-with-throwaway-key \
  [--max-order-notional 1.00 --max-daily-notional 2.00] [--cash 20]
```

| Forecaster | No caps, cash 1000 | Caps 1.00 / 2.00, cash 1000 | Caps, cash 20 |
|---|---|---|---|
| logistic (0.461) | approved: buy 8 YES at 0.42 (Kelly 0.1 × full fraction 3.8%, target 3.82); one signed POST, then the dry cancel | risk approved 8; gateway refused `local:order_notional_cap` (8 × 0.42 = 3.36 > 1.00); nothing built | risk refused `size_zero` (Kelly target 0.08, below one contract) |
| gradient boosting (0.438) | no intent: `edge_below_threshold` | same | same |
| symbolic (0.413) | no intent: `edge_below_threshold` | same | same |

What this shows:
- The forward-shadow forecast becomes a well-formed, signed demo request. Signing used
  a throwaway key, and nothing was sent.
- The gateway's notional cap refuses an oversized order before it is built.

What it does not show: acceptance, fills or profit. The risk limits were
`simulation_defaults(cash)`, not canary limits. The runbook now asks for canary limits
within the caps (#35, `67b260a`).

## Settlement and evaluation

Pending: the bounded `shadow-evaluate` runs at about 05:15Z on 2026-10-20. Its results
will be added here.
