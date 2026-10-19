# Roadmap run handoff (2026-10-18/19)

Final handoff for the research-to-trading roadmap run (Opus 5.5 throughout, including
subagents). Everything is a stacked **draft** PR; nothing is merged, deployed or
activated. No order was placed, no credential exists in this system, no paid call was
made, and production was only read with short read-only queries (never copied).

A functional pipeline is not evidence of edge, and nothing here shows profitability:
on real historical data **no model beats the market midpoint**, and every simulated
profit in this document is synthetic or scenario-labelled.

## 1. Draft PRs (stack order; each targets the previous)

Base: PR #16 `feat/forward-shadow-ops` @ `82665cd02a9db9694f957eca32537cca5cf71c31`.

| PR | Branch | Head (full SHA) | Scope | Suite at head |
|---|---|---|---|---|
| #17 | `fix/record-contract-3` | `f8642bf34fefc229470b4417a97acf818c3a9986` | A1 contract versioning (D-001), foundation repair | 835 passed |
| #18 | `feat/eligibility-rule-v2` | `622caadd47d2047139c7c994b3c831abf1f57dca` | A2 rule v2 for date-only postseason rules (D-002) | 868 passed |
| #19 | `feat/schedule-revisions` | `74a60a8057467bd4a6f34082925cfa9389d481c0` | A3 post-C schedule changes unscored (D-003) | 873 passed |
| #20 | `feat/historical-acquisition` | `2f99f6101d429cf41fc2bf4e2df1e27d6b588bf5` | A4 public historical acquisition | 881 passed |
| #21 | `feat/datasets-models` | `09ecb35522bb8781a8fa22b5eadbb79a7785e936` | B features, datasets, registry, models, training | 953 passed |
| #22 | `feat/registered-forecasters` | `007a9d3a983497c534306e214817eb47dbaf5459` | B4 decision contract 4, evaluation contract 2 (D-004) | 960 passed |
| #23 | `feat/symbolic-search` | `00eb7376c1af21b6d8a23267b8f4cb04d8e09442` | C bounded symbolic regression | 1022 passed |
| #24 | `feat/paper-execution-risk` | `e70da2b6ba6ea14a67054ce460d7273ed4f8fdc4` | D/E ledger, simulator, risk, Kelly (D-006) | 1105 passed |
| #25 | `feat/kalshi-live-adapter` | `25a4a8d38b51219e57d1f9451f0587853bd70193` | F live adapter, disabled by default (D-007) | 1209 passed |
| #26 | `feat/history-backtest` | `1e02613ef1ce79f2bc473c3fb5537f3b1d7072b4` | D/H candle backtest, live dry run (D-010, D-011) | 1217 passed |
| #27 | `feat/research-pods` | `145735cf6ba04820870e181a5ac02bff5050ffe7` | G orchestration, operations, protected evaluator (D-008) | 1272 passed |
| #28 | `feat/integration-demos` | final head in §11 | H demos, ops model registry, run documents | 1275 passed |
| #29 | `perf/ledger-row-cache` | `26dceed2f8fad8b22320845b40d6d3b755265057` | ledger parsed-row reuse (D-012); read-only registry for non-holdout backtests | 1279 passed |
| #30 | `feat/pod-autopilot` | `d744766d96ebb869a9e8ab6ab1fc1c40e9ca5262` | G pod autopilot: development-only planners, bounded loop (D-013) | 1294 passed |
| #31 | `fix/review-26-27` | `e4b6770baf7332697720819bd86a0a8a7e13ea6c` | repairs from the #26/#27 pre-review: pinned holdout (D-014), input provenance, candidate-bound evidence, dry-run fidelity | 1303 passed |
| #32 | `fix/review-24-25` | `3827df2adea441b33bdef56d0a48c226d5e89aa7` | repairs from the #24/#25 pre-review: skew latch, TLS trust, ambiguous writes, exits, ingest idempotency, Kelly rounding | 1317 passed |

"Suite" = `pytest -q` at that exact head with the two pre-existing `tmux_jev` observer
tests deselected (the optional tool is not installed; they fail on `main` too), measured
2026-10-19 in a clean verification worktree; `ruff check src tests ops` clean at every
head.

## 2. Status by requirement

Full matrix: `docs/roadmap/requirements-matrix.md`.

| Status | Items |
|---|---|
| **Demonstrated on real data** | rule v2 on the real archive (60 rule pairs) and live (1 game, 2 eligible contracts, 2026-10-19); historical acquisition (2025 + 2026, with 2026 execution windows); datasets; training of logistic, gradient boosting and symbolic models; research pods and the protected evaluator on 2026 and on 2025→2026; the pod autopilot (heuristic planner) on the 2026 snapshot; candle vs collected book at C; paper backtest (simulated execution) on real 2026 validation candles through pods and the coordinator, which agree exactly, and on the two-season validation set |
| **Demonstrated on synthetic data (asserted in the suite)** | full chain proposed → validated → shadow_tested → paper_approved with hand-checked numbers; one-time holdout in both ledgers; dry-run order request; recovery from worker and coordinator SIGKILL, backtest crashes, live create timeout |
| **Implemented, simulated only** | paper simulator, risk engine, Kelly; Kalshi live adapter against a deterministic exchange double; dry-run gateway |
| **Pending tonight (bounded jobs running)** | live rule-v2 forward decision at C = 23:08Z for `KXMLBGAME-26OCT192008CLETB`; live forward shadow with the three 2026 pod models as registered forecasters |
| **Untested** | the live canary (`docs/live-canary-runbook.md`, never executed); exchange acceptance of any request; fee schedules against real fills |
| **Blocked** | A5 NBA/NFL (D-005: no authorized official free schedule source) |
| **Not done by decision** | one-time holdout of a final candidate (D-009: no candidate validated; the 2026 holdout is untouched, and since #31 it is enforced: the real datasets are pinned at 2026-09-02, D-014) |

## 3. Real-data findings (historical retrieval; not forward evidence; not edge)

- **2026 only** (develop ≤ 2026-07-31, validate August, 401 rows): logistic +0.0051,
  gradient boosting +0.0143, symbolic (`pair_mid_c`) +0.0023 log loss vs market mid.
  All rejected.
- **Two seasons** (develop on 2025, validate on 2,027 rows of 2026): logistic +0.0004
  (CI −0.0031, +0.0039), gradient boosting +0.0095 (CI +0.0035, +0.0158), symbolic
  (`logit_mid_c`) −0.0004 (CI −0.0017, +0.0010, halves disagree). All rejected.
- The symbolic search's best expressions are the market's own midpoint or log-odds.
- The pod autopilot on the 2026 snapshot refined grids on development cross-validation
  over three rounds and then stopped. Both best models were rejected by the protected
  evaluator: gradient boosting is significantly worse than market mid (+0.0121, CI
  +0.0015 to +0.0232), and the symbolic `tanh(logit_pair_mid_c)` is the market itself.
- Candle vs collected book at C (12 postseason games): mid and spread identical; book
  best-level sizes 11k–5.9M (archive units).
- Paper backtest on real 2026 validation candles. This is a simulation, and selection
  biased.
  - Without assumed depth nothing fills, because candles have no sizes.
  - Under assumed depth 5 or 25, every scenario with fills has a negative point
    estimate after fees. Before fees all but one are negative too: gradient boosting at
    depth 5 under the 0.5× schedule is +3.32. For example, gradient boosting at depth 25 on
    the general schedule loses 67.50 after fees (43.45 of fees) on a $1,000 bankroll.
    Every 95% interval, from a bootstrap over markets, contains zero.
  - One extra slippage tick means no fill (the limit is the decision-time ask), and
    latency below one minute changes nothing.
  - In one run, the risk engine's daily loss stop engaged the kill switch, and that
    stopped the run.
  - The pods (verified input copies, no archive access) and the coordinator produced
    identical numbers in all 36 shared scenarios.
- Paper backtest on the two-season validation set (2,027 games, one ledger per candidate
  and depth). The signs are mixed:
  - logistic at depth 5: +2.28 after fees (CI −41.52, +48.96);
  - gradient boosting at depth 25: −36.90 after fees (CI −361.18, +319.65), but +146.18
    before them.
  - Every interval for rows with more than one trade contains zero; the symbolic model
    traded once, so its rows have no meaningful interval. Fees are the largest systematic
    cost.
  - The 15% drawdown stop engaged the kill switch in both depth-25 runs.
  - The near-market symbolic model traded once.

Details: `docs/evidence/real-data-demo-20261019.md`.

## 3b. Independent pre-review (before Codex)

Three read-only Opus reviewers examined the newest code (#26, #27, #29, #30) and probed
each finding in temp directories.

- **#29 (ledger cache):** judged sound.
- **#30 (autopilot):** the confirmed findings were fixed in `d744766`: malformed provider
  values, a non-finite-JSON gap, the record ordering, the budget across runs and fold
  comparability.
- **#26 and #27:** one critical gap, the unpinned holdout, and several major ones:
  - forgeable pod inputs and diagnostics run on another snapshot;
  - a silent constant baseline;
  - unbound paper evidence;
  - holdout ledgers that were not crash-safe;
  - holdout results written before their ledger entry;
  - phantom dry-run exposure and an overstated dry-run claim.

  All confirmed findings are fixed in #31.
- **#24 and #25:** no critical finding. The activation boundary, secret handling, write
  semantics, Decimal money and the accounting identity all held.
  - Fixed in #32:
    - a stale or cached `Date` header that could stop shutdown from cancelling;
    - the TLS trust store following the environment;
    - ambiguous 4xx answers to order creation;
    - notional caps blocking exits;
    - sell coverage ignoring recent fills;
    - startup reconciliation completing early;
    - risk-limit and environment bindings;
    - non-idempotent resent venue events;
    - fees above their bound;
    - a Kelly rounding edge case.
  - Documented, not changed:
    - live settlement recording does not exist yet;
    - late fills on settled tickers;
    - fractional quantity steps over-reserving fees;
    - reduce-only sells under unknown state (by design; guarded by venue coverage).

Codex review remains the user's step.

## 4. Reproduction

```sh
cd /Users/ashwin/Documents/github-repos/Vader-Intelligence-roadmap   # or any checkout of the PR head
/Users/ashwin/Documents/github-repos/Vader-Intelligence/.venv/bin/uv sync --locked
.venv/bin/ruff check src tests ops && .venv/bin/ruff format --check src tests ops
.venv/bin/python -m pytest -q \
  --deselect tests/worker_observer/test_observer.py::test_upstream_pin_guard_and_cli_help \
  --deselect tests/worker_observer/test_observer.py::test_bridge_assessment_uses_frozen_input_with_mock_provider
.venv/bin/python tests/integration/e2e_demo.py         # synthetic, ~22 s, prints hand-checks
.venv/bin/python tests/integration/recovery_demo.py    # synthetic, ~14 s
# wheel (venv outside the checkout)
UV=/Users/ashwin/Documents/github-repos/Vader-Intelligence/.venv/bin/uv
$UV build --wheel -o /tmp/vi-dist && $UV venv -p 3.11 /tmp/vi-wheel && \
  VIRTUAL_ENV=/tmp/vi-wheel $UV pip install /tmp/vi-dist/*.whl && \
  /tmp/vi-wheel/bin/python tests/integration/wheel_smoke.py --python /tmp/vi-wheel/bin/python
```

Real data (needs the research data directories; production opened read-only):

```sh
R=/Users/ashwin/Documents/Vader-Intelligence-research
.venv/bin/python -m vader_intelligence.cli dataset build --registry $R/registry --source history \
  --history-db $R/history/history.sqlite3 --feature-set fs-market-v1 --from 2025-03-27 \
  --through 2026-10-31 --name mlb-2025-2026-history-fs-market-v1 --identity-rules default,history-date-only
.venv/bin/python tests/integration/real_data_demo.py pods --registry $R/registry \
  --dataset 8bdc1e113fb674381b58faf14133058c15a1bbbac9e26f446dcf0ac65d6f7d0f \
  --root $R/pods-2025-2026 --out $R/demo/real-data-pods-2025-2026.json \
  --development-through 2025-12-31 --validation-through 2026-08-31
.venv/bin/python tests/integration/candle_vs_book_check.py \
  --production ~/Documents/Vader-Intelligence-operations/20260930-first-deployment/production.sqlite3 \
  --history-db $R/history/history.sqlite3 --from 2026-09-28 --through 2026-10-31 --out /tmp/check.json
```

## 5. Test results (recorded)

| Head | Result |
|---|---|
| #17–#25 | 835, 868, 873, 881, 953, 960, 1022, 1105, 1209 passed (2 deselected each); ruff clean |
| #26 | 1217 passed, 2 deselected |
| #27 | 1272 passed, 2 deselected |
| #28 | 1275 passed, 2 deselected; wheel smoke complete (schemas 1–6); ops 43 passed |
| #29 | 1279 passed, 2 deselected; the real 2026 scenario gives the same report apart from `code_hash`, with CPU time down from 78 s to 16 s |
| #30 | 1294 passed, 2 deselected (15 new autopilot tests); wheel built at this head imports `pods.planner` and `pods.autopilot`, and its smoke test is complete (schemas 1–6). A read-only Opus review of #29 and #30 found nothing critical; its confirmed findings on #30 were fixed in `d744766` |
| #31 | 1303 passed, 2 deselected (9 new tests); wheel built at this head passes its smoke test (schemas 1–6) |
| #32 | 1317 passed, 2 deselected (14 new tests); wheel built at this head passes its smoke test (schemas 1–6) |

## 6. Data and model limitations

- Historical retrieval happened after the fact; candles are one-minute summaries without
  sizes; execution at arrival is unverified (D-010).
- All 2025 rows rely on date-only identity: the start is the official schedule's, not
  corroborated by contract text (`start_corroborated = false`).
- Validation is selection-biased: several proposals per snapshot (recorded in every
  evaluator record); the CI-and-halves rule is the only multiplicity guard.
- Candle-vs-book agreement is shown on 12 postseason games only; production did not
  collect regular-season books.
- Fee schedules are unverified (higher-fee default); Kalshi API facts marked unverified
  are listed in `docs/live-canary-runbook.md` §6 (A1–A20).
- Holdout-period data that was read:
  - The candle-vs-book check (§3) read market *features* (mid, spread) at C for 16
    postseason games dated after the 2026-09-02 holdout pin. It read no outcome and
    scored no model.
  - Tonight's forward shadow scores live games in that date range. Those games are
    forward evidence, not dataset rows.
- Backtest execution: candles cannot resolve sub-minute latency. Slippage is a worse fill
  price that must stay within the limit; with take-at-ask limits, one tick means no
  fill rather than a worse price. Paper runs cost time quadratic in the input count
  (the ledger projection is rebuilt after every write), so the two-season backtest ran
  single scenarios instead of the full grid.

## 7. Live-activation gates (all must pass; activation is the user's decision)

`trading/kalshi/activation.activate_live` requires: acknowledged SHA-256 of the
activation file; `enabled = true`; environment demo|production; key id; secret reference
(file 0600 or Keychain) with no key material in the file; per-order and daily notional
caps; market rules with price ranges; a live, consistent ledger; complete risk limits
whose hash matches the file; an explicit operator kill-switch disengage not in the
future; then the credential is read last. No CLI path activates; `vader live readiness`
checks every local gate without the key. Runbook: demo first, one far-from-touch
post-only order, cancel, reconcile, shutdown (never executed).

## 8. Outstanding review decisions for the user

1. Review order: #17 (foundation) first; each PR is independent enough for Codex Prime
   and Codex Secondary per `docs/roadmap/review-packets.md`.
2. D-005: accept ESPN or provide an authorized source to unblock NBA/NFL.
3. D-009: whether a two-season split should define a new holdout before further model
   work; the current 2026 holdout stays untouched.
4. D-008 default criteria (CI below zero and both halves) — stricter or looser?
5. Backtest slippage semantics: keep "slippage = no fill at a touch limit", or add a
   strategy limit offset (pay up to k ticks) so the slippage axis measures price?
6. Live activation (demo canary) — only after review; requires the user's own key.

## 9. Cleanup (not done automatically)

- Background jobs: live rule-v2 driver (`/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-rule-v2`), model shadow driver (`.../20261019-models`); both end by themselves (last ≈05:20 UTC 2026-10-20). The acquisitions and backtests have finished.
- Worktrees (with their venvs): `Vader-Intelligence-wt-{deps,execution-risk,symbolic-engine,research-pods-core,models-library,live-adapter}` (finished subagent work, integrated), `-live-v2`, `-live-models`, `-history-run`, `-history-run2` (pinned run code), `-verify` (holds `feat/pod-autopilot`, #30; also used for head verification). Local branches `wip/deps`, `feat/execution-risk`, `feat/symbolic-engine`, `feat/research-pods-core`, `feat/models-library`, `feat/live-adapter` are superseded by the stack.
- Data directories (outside the repo): `/Users/ashwin/Documents/Vader-Intelligence-research/{history (≈0.3 GB),registry,pods-2026,pods-2025-2026 (with backtest-ledgers/),demo}`, `/Users/ashwin/Documents/Vader-Intelligence-shadow/{20261019-rule-v2,20261019-models}`, the session scratchpad (profiles, single-run ledgers, the wheel venv).
- Research pods added in `pods-2026` by the autopilot demos: `auto-baseline`, `auto-symbolic` (2 rejected candidates) and `auto2-*` (planning only). The orchestration store is append-only, so they stay as history.

## 10. Next highest-value work

1. Review and merge decisions for #17–#20 (the evidence foundation).
2. Forward evidence: keep forward shadows with registered forecasters running through the
   rest of the postseason and next season; forward data is the only unbiased test.
3. Features beyond the market: the market-only feature set cannot beat its own midpoint by
   construction; team/pitcher/lineup features with forward availability are the next
   research question (the `fs-full-v1` team features exist but are research-only).
4. Execution realism: collect order books (not candles) for the trading window to
   measure depth at arrival before any canary.
5. Simulator cost.
   - #29 (D-012) removes the re-parsing: committed ledger rows are parsed once. On a
     401-row scenario, CPU time falls from 78 s to 16 s.
   - The projection is still recomputed in full after every write (15 of 42 profiled
     seconds), so a paper run is still quadratic, with a much smaller constant.
   - An incremental projection is the remaining step. It changes how the authoritative
     ledger derives state, so it needs its own review.
6. Before any live use: record settlements in the live ledger, and accept late fills
   timestamped before a settlement. #24/#25 pre-review, documented but not changed.
7. Only after forward evidence of edge: the demo canary per the runbook.

## 11. Final head of #28

Recorded when the last evidence commit lands (see the PR's head on GitHub; this file is
part of that commit, so its own SHA cannot appear in it).
