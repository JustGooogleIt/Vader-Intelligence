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
