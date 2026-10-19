# Real-data demonstration, 2026-10-19

Real but **historical** evidence: Kalshi public historical listings and one-minute candles
for the 2026 MLB season, retrieved after the fact (history archive, interpretation
history-v1), joined to the official MLB schedule. Nothing here is forward evidence, and
nothing is evidence of edge. The final holdout (official dates after 2026-09-01) was
**not** read.

## 1. Research pods on the 2026 dataset

Dataset `a903238860b5c6bb2c07b3777421c559e0e4cd67f9fb8e110980fca7b9f94435`
(`mlb-2026-history-fs-market-v1`, forward-capable market features). Split: development
≤ 2026-07-31, validation 2026-08-02..2026-08-31 (1-day embargo). Snapshot
`77ffe832cba58405d1f8ae4a1ea7f9e845206075f4da57c660e5c384aff64b4a`: 1,611
development rows, 401 validation rows. Commands and outputs:
`docs/evidence/real-data-pods-20261019.json`, produced by
`tests/integration/real_data_demo.py pods ...` through the operator CLI (`vader pods
...`).

| Candidate | Pod output | Validation log loss (recomputed by the evaluator) | Market mid | Paired delta (95% CI) | Halves | Decision |
|---|---|---|---|---|---|---|
| logistic (`d316e849…`) | C = 0.01 (4 configs, exhausted) | 0.67613 | 0.67105 | +0.00508 (−0.00032, +0.01086) | −0.0023, +0.0115 | rejected: does not beat the baseline |
| gradient boosting (`a6d69da1…`) | 7 leaves, 100 iterations (8 configs) | 0.68532 | 0.67105 | +0.01426 (−0.00004, +0.02930) | +0.0014, +0.0254 | rejected |
| symbolic (`06891d03…`) | `pair_mid_c` (complexity 1; 2,907 candidates; stopped at the generation limit) | 0.67331 | 0.67105 | +0.00226 (−0.00548, +0.00959) | −0.0031, +0.0069 | rejected |

Three proposals were made on this snapshot; every evaluator record carries that count.
The symbolic search's best expression is the market's own pair midpoint (linearly
recalibrated), which is the honest outcome: on this data nothing found improves on
the market. Robustness pods agree on every slice (August, regular season only, tight
spreads; the wide-spread slice has 4 rows): each candidate is worse than mid on point
estimate. Dropping `mid_c` from the logistic and gradient-boosting inputs makes them
worse still (+0.0076 and +0.0164).

## 1b. Two seasons: develop on 2025, validate on 2026

The follow-up acquisition added the 2025 season (2,201 events, 2,101 identified, 8,404
candle requests, none failed). 2025 rules carry no start time, so every 2025 row comes
from the opt-in `history-date-only` identity rule: the start is the official schedule's,
not corroborated by the contract text (`start_corroborated = false` on all 2,101 rows).
Dataset `8bdc1e113fb674381b58faf14133058c15a1bbbac9e26f446dcf0ac65d6f7d0f`
(`mlb-2025-2026-history-fs-market-v1`, 4,506 rows). Split: development ≤ 2025-12-31
(2,101 rows, all of 2025), validation 2026-01-02..2026-08-31 (2,027 rows), holdout after
2026-09-01 (363 rows, **not read**; same dates as above). Snapshot
`701f77c4a29a78e8f1428ff98fea3fa9024bcd42f33a96f344ec55a71ed69904`. Commands and
outputs: `docs/evidence/real-data-pods-2025-2026-20261019.json`.

| Candidate | Validation log loss | Market mid | Paired delta (95% CI) | Halves (split 2026-06-13) | Decision |
|---|---|---|---|---|---|
| logistic (`d9d84aa4…`, C = 0.01) | 0.68247 | 0.68207 | +0.00040 (−0.00307, +0.00388) | −0.0026, +0.0034 | rejected |
| gradient boosting (`86646fec…`) | 0.69154 | 0.68207 | +0.00947 (+0.00347, +0.01584) | +0.0051, +0.0138 | rejected: significantly worse |
| symbolic (`1ee191c4…`, `logit_mid_c`, 3,212 candidates) | 0.68165 | 0.68207 | −0.00042 (−0.00172, +0.00104) | −0.0017, +0.0009 | rejected: CI crosses zero, halves disagree |

With five times the validation rows the conclusion sharpens rather than changes: the
logistic model is indistinguishable from the market, the gradient-boosting model is
significantly worse, and the symbolic search's best expression is a recalibration of the
market's own log-odds (a 0.04% improvement that is not significant and reverses in the
second half). Robustness slices by month agree (logistic: negative in April–June, positive
in July–August; gradient boosting positive every month). No candidate reached
`validated`, so no holdout was consumed (D-009).

## 2. Candle vs collected book at C (postseason)

`tests/integration/candle_vs_book_check.py` compared candle-based features at C (history
archive) with features from order books the production collector retrieved at or
before C (production archive, opened read-only), for official dates 2026-09-28..
2026-10-31 with identity rules `default,history-date-only` (postseason identities are
date-only, `start_corroborated = false`). Output:
`docs/evidence/candle-vs-book-20261019.json`.

- 28 candle rows, 16 book rows, 16 games in both. 12 games have both mids (the other 4
  have no book quote fresh enough at C).
- `mid_c` and `spread_c`: identical on all 12 (max |difference| 0). `pair_mid_c`: max
  |difference| 0.005 (one game), mean 0.0004.
- Best-level displayed sizes in the last book before C range 11,209 to 5,908,058
  (median 1,130,152, archive fixed-point units), so the backtest's 5- and 25-contract
  assumed-depth scenarios are far below displayed depth. Displayed depth at C is not
  evidence of execution at arrival.

Interpretation: on these 12 postseason games the history assumption (a candle's closing
bid/ask is the book's best bid/ask at the candle's end) holds exactly at C. The sample is
small and postseason-only; regular-season games were not collected by production.

## 3. Paper backtest on real validation candles

Pending: needs the execution-only `hist-candles-post` stage for the 2026 validation rows
(follow-up acquisition running). Command when it is in:
`tests/integration/real_data_demo.py backtest --registry ... --dataset ... --root ...
--history-db ... --out ...`.
