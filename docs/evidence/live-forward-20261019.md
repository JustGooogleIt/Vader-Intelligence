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
  - cycles in fixed 120 s slots from 22:30:10Z; each started 1–4 s into its slot,
    because the driver polls every 5 s.
- **Model shadow** (`/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-models`):
  - code `ab43c48` (in #28, before the #31 and #33 repairs);
  - decision contract 4, with three registered forecasters, all trained by research
    pods on the 2026 history dataset;
  - the forecasters are logistic `d316e849`, gradient boosting `a6d69da1` and symbolic
    `06891d03`;
  - **all three were rejected by the protected evaluator**, since none beats the
    market midpoint on validation;
  - cycles in fixed 120 s slots from 21:56:10Z.
- Both shadows: cadence 120, collector deadline 90, grace 5, tick budget 15. One
  bounded `shadow-evaluate` runs after the game, on 2026-10-20 at 05:00Z for rule v2
  and 05:15Z for the models.
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

The symbolic model is a logistic function of a linear map of the standardized pair
midpoint (artifact parameters a = 0.2239, b = −0.1577), so it stays near the market.
The logistic model is 4.6 points above the market mid of 0.415.

## The midpoint abstention (D-016)

| Book for the CLE contract | Retrieved | First witnessed |
|---|---|---|
| models shadow | 23:04:15.6 | 23:06:11.9 (≤ C) |
| models shadow | **23:06:15.8** | **23:08:12.2 (after C)** |
| rule-v2 shadow | 23:04:15.5 | 23:06:11.9 (≤ C) |
| rule-v2 shadow | 23:06:15.7 | 23:08:12.2 (after C) |

Specification §4 selects the latest book received **and** witnessed by C. Here that is
the 23:04:15 book, 224 s old at C. The implementation (selection-v1) anchors instead on
the latest book *received* by C, which here is the 23:06:15 book. That book was
witnessed only by the next tick, after C, so the midpoint abstained in both shadows.
Each cycle runs the tick before it collects, so in normal operation this happens at
almost every cutoff.

This is a deviation from the specification, recorded in `docs/roadmap/decisions.md`
D-016. It is repaired as an opt-in `selection_version = "selection-v2"` in #36, with
the default unchanged. Read-only on these archives at C, selection-v1 reproduces the
recorded abstention. Selection-v2 selects the 23:04:15 book (witnessed 23:06:11), with
midpoint **0.4150** in both shadows. Tonight's stored decisions are unchanged: they
were made, and remain, under selection-v1.

**Point in time.** No forecaster used evidence witnessed after C. All 70 listing
quotes the models used came from fetches witnessed by C; the decision records 20 of
the fetch IDs. The latest was retrieved at 23:04:15 and witnessed at 23:06:11. The
23:06 listing fetches were first witnessed at 23:08:12, after C, and were not used.

## D-015 (`last_trade_gap_c`)

Forward value: 0.005, present rather than `None`. On 2026 history the feature is
never `None`, with |value| median 0.005 and p99 0.01. On 2025 history it is `None` in
6.4% of rows. This one forward value lies inside the history distribution, but a
single row cannot show whether the two definitions agree, so D-015 stays open.

## Live-adapter dry run on the real forward decision

Command (code: the #35 head `7686513`; the dry-run path is unchanged since #33). The
dry run evaluates at C using the latest archived book received by C (23:06:15.8). That
book was first witnessed after C, so the forecasters could not have used it. Its top of
book (0.41/0.42) equals that of the 23:04 book, so the outcome would be the same.

```sh
vader live dry-run --archive <model shadow>/shadow.sqlite3 --forecaster model:<id> \
  --ledger <new paper ledger> --sign-with-throwaway-key \
  [--max-order-notional 1.00 --max-daily-notional 2.00] [--cash 20]
```

| Forecaster | No caps, cash 1000 | Caps 1.00 / 2.00, cash 1000 | Caps, cash 20 |
|---|---|---|---|
| logistic (0.461) | approved: buy 8 YES at 0.42 (Kelly 0.1 × full fraction 3.8%, target 3.82); one signed POST, then the dry cancel | risk approved 8; the dry-run gateway's copy of the live per-order cap refused it (`local:order_notional_cap`, 8 × 0.42 = 3.36 > 1.00) before it was signed or recorded | risk refused `size_zero`; derived: 0.1 × 3.8% × 20 ≈ 0.08 target, below one contract's cost of 0.44 (the output records no sizing) |
| gradient boosting (0.438) | no intent: `edge_below_threshold` | same | same |
| symbolic (0.413) | no intent: `edge_below_threshold` | same | same |

What this shows:
- The forward-shadow forecast becomes a well-formed, signed demo request. Signing used
  a throwaway key, and nothing was sent.
- The gateway's per-order notional cap refuses an oversized order before it is signed
  or recorded.

What it does not show: acceptance, fills or profit. The risk limits were
`simulation_defaults(cash)`, not canary limits. The runbook now asks for canary limits
within the caps (#35, `67b260a`).

## Settlement and evaluation (2026-10-20)

Each driver ran one bounded `shadow-evaluate`: rule v2 at 05:00Z and the models at
05:15Z. Each one refreshed settlement evidence and then ran `research run --view
forward-shadow` over 2026-10-20.

- **Settlement step:** `partial` in both shadows. The 20-target limit truncated the
  selection; the archive also holds other markets. The game was observed settled:
  outcome `binary_scorable`, **y = 0** for the CLE contract, so Tampa Bay won.
- **Evaluation:** `inconclusive` in both, which is the expected status when the paired
  comparison has no rows.
  - One decision (U = E = K = 1), published timely, with the constant scored.
  - The midpoint abstained (D-016), so there is no paired midpoint score (n = 0).
  - The driver exit code 1 comes from the partial settlement step.

| Forecaster (P of CLE) | Brier | Log loss | Source |
|---|---|---|---|
| constant 0.5 | 0.2500 | 0.6931 | evaluator, both shadows |
| symbolic 0.413415 | 0.1709 | 0.5334 | evaluator, model shadow |
| gradient boosting 0.438094 | 0.1919 | 0.5764 | evaluator, model shadow |
| logistic 0.461393 | 0.2129 | 0.6188 | evaluator, model shadow |
| midpoint | — | — | abstained under selection-v1 |
| *midpoint 0.4150 under selection-v2* | *0.1722* | *0.5361* | *derived here from the read-only selection-v2 result (D-016); not an evaluator score* |

This is **one game**. It says nothing about any model's skill. All three models were
rejected on validation, and the market mid (0.415, by the derived selection-v2 figure)
is essentially tied with the best of them here.

The logistic model's dry-run order (8 YES CLE at 0.42) would have lost its stake had it
been placed and filled. It was never sent, and a fill was never simulated.
