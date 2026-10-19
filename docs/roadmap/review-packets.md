# Review packets for the roadmap PR chain (Codex Prime and Codex Secondary)

Two independent reviewers. Each packet lists what to read first, the invariants to
attack, and commands that reproduce the evidence. Both reviewers inspect the interfaces
listed last. Exact SHAs: `docs/roadmap/continuation.md`. All PRs are drafts; nothing is
merged, deployed or activated; no credentials exist in this system.

| PR | Branch | Scope | Prime | Secondary |
|---|---|---|---|---|
| #17 | `fix/record-contract-3` | D-001 contract versioning (foundation repair) | lead | wheel/ops |
| #18 | `feat/eligibility-rule-v2` | D-002 rule v2 + mapping policy 2 | lead | live smoke |
| #19 | `feat/schedule-revisions` | D-003 post-C schedule change disposition | lead | — |
| #20 | `feat/historical-acquisition` | A4 public historical source | evidence kind | transport/CLI/resume |
| #21 | `feat/datasets-models` | B features, datasets, registry, models, training | leakage, holdout | CLI, determinism |
| #22 | `feat/registered-forecasters` | D-004 decision contract 4, evaluation contract 2 | witnessed inputs, scoring | migration 006, CLI |
| #23 | `feat/symbolic-search` | C bounded symbolic search | selection bias, holdout | budgets |
| #24 | `feat/paper-execution-risk` | D/E ledger, simulator, risk, Kelly (D-006) | accounting, risk math | crash/restart |
| #25 | `feat/kalshi-live-adapter` | F disabled-by-default Kalshi adapter (D-007) | duplicate exposure | secrets, activation gates |
| #26 | `feat/history-backtest` | D/H candle backtest, dry run (D-010, D-011) | execution realism, holdout ledger | no network, paper-only |
| #27 | `feat/research-pods` | G orchestration, operations, protected evaluator (D-008) | protected state, criteria | process bounds, determinism |
| #28 | `feat/integration-demos` | H demos, ops model registry, run docs | claims | reproduction |
| #29 | `perf/ledger-row-cache` | ledger parsed-row reuse; read-only registry for non-holdout backtests (D-012) | append-only soundness | concurrency |
| #30 | `feat/pod-autopilot` | G autonomy: development-only planners, bounded autopilot loop (D-013) | context leakage, selection | bounds, resume, provider refusal |
| #31 | `fix/review-26-27` | repairs from the #26/#27 pre-review: pinned holdout (D-014), input provenance, candidate-bound evidence, dry-run fidelity | holdout pin, evidence binding | dry-run checks, quarantine |
| #32 | `fix/review-24-25` | repairs from the #24/#25 pre-review: skew latch, TLS trust store, ambiguous write answers, uncapped exits, ingest idempotency, Kelly rounding | ingest identity, fee bound | live adapter safety |
| #33 | `fix/review-17-23-28` | repairs from the #17–#23 and #28 pre-review: stale settlement mapping (#19), forecast-tick abstention (#22), synthetic label (#20), ops guard | outcome join, abstention | ops guard |
| #34 | `feat/live-settlements` | F live settlement recording (venue settlements recorded only on an exact match with the ledger) | payout and counts identity | allowlist, cursor, fail-closed reasons |

## Codex Prime — evidence meaning, versioning, leakage, scoring, money correctness

Read first: `docs/roadmap/decisions.md` (D-001..), `docs/roadmap/evidence-index.md`.

1. **D-001 (#17).** Is every rule difference between `rules="draft"` and `rules="current"`
   exactly a PR #12 hunk (`git diff 5811991 27a3473 -- src/vader_intelligence/forecast`)?
   Can any `legacy_draft` row become operational proof (`evidence.PROOF`, witness dedup,
   tick binding, evaluator publication)? Can a contract-3 row ever be validated under the
   draft rules? Does migration 005 touch any row? Do the legacy archives in
   `tests/fixtures/legacy_archives` truly come from the named revisions (regenerate with
   `generate.py`; structure and classification, not bytes, must match)?
2. **D-002 (#18).** Can rule v2 admit a game whose official start differs from the ticker
   time? Can close/expiration/occurrence times influence eligibility? Is the default
   cohort hash unchanged (`Config.provenance()`)? Does settlement replay re-derive mappings
   from the recorded rule set, not the caller's?
3. **D-003 (#19).** Can a decision for a game later played at another start ever be scored?
4. **A4 (#20).** Can historical-retrieval data reach the forecast tick or any forward
   cohort? (Stages are distinct; selection reads `book` only.)
5. **B.** Leakage: can a feature see a quote after C (`FeatureContext`, `until`), a result
   from the game date or later, a label, or post-cutoff execution candles
   (`hist-candles-post` is read only by `interpret.execution_candles`)? Preprocessing,
   calibration and search fit only on training folds; OOF predictions never from a model
   that saw the row; holdout read only through `splits.holdout_rows`; the holdout ledger
   marks every evaluation after the first as reuse.

6. **D-004 (#22).** Can a model feature use a quote not witnessed by C, or a research-only
   (non-forward) feature set run forward? Is a forecaster scored on exactly the baselines'
   population? Does a protocol without forecasters keep its hash?
7. **D-006 (#24).** Money: is every amount Decimal and on grid? Can a paper event reach a
   live ledger? Can `submit_unknown` be resolved other than by reconciliation? Kelly: hand
   cases and the clip to limits; does the risk engine ever approve with an unknown state?
8. **D-007 (#25).** Can any path create a network transport or read the credential without
   passing every gate? Can a lost create response lead to two venue orders?
9. **D-010 (#26).** Can a backtest fill without an assumed depth? Can the holdout segment
   run without a ledger entry? Is a model trained on another split refused?
10. **D-008 (#27).** Can a worker read anything but its staged inputs, or can a snapshot
    carry a holdout row? Can pod output reach promotion evidence? Are the evaluator's
    criteria applied to recomputed values only? Can a candidate skip a state or be decided
    twice? Is the holdout consumed at most once per candidate and only from `validated`?
11. **Claims (#28).** Every number in `docs/evidence/real-data-demo-20261019.md` and the
    E2E demo must trace to a recorded output; no profitable simulation is described as
    edge; D-009 (holdout preserved) is the right call.
12. **Real-candle backtest (#26, #28).** Several checks:
    - The 2026 grid shows no fills without assumed depth and none with one slippage tick
      (`simulator._executable`: the fill price plus slippage must stay within a
      take-at-ask limit). Is that the intended meaning of the slippage axis?
    - Are pod and coordinator results equal because the inputs are equal (same execution
      payload hash), not by coincidence?
    - Is the kill-switch stop in the depth-25 run the risk engine's documented behaviour
      (auto-engaged on `daily_loss_stop`, never auto-disengaged)?
    - `Ledger.projection()` is rebuilt after every write (quadratic paper runs). Is an
      incremental projection safe to add in its own PR?
13. **Autopilot (#30).** Several checks:
    - Can any validation-derived value reach `planner.context` (the allowlist,
      `VISIBLE_FIELDS`)?
    - Can a provider proposal reach `store.submit` without `planner.validate` and the
      operation contract?
    - Can the loop submit beyond its bounds?
    - Is one accepted candidate per pod the right multiplicity control? The evaluator
      records the snapshot's proposal count.
    - A pre-review by an Opus agent found nothing critical. Its findings on #30 were
      fixed in `d744766`: malformed provider values, a non-finite-JSON gap, the record
      ordering, the budget across runs and fold comparability.
14. **Pinned holdout and pre-review fixes (#31).** Several checks:
    - Can any path read or score rows on or after a dataset's pin without a ledger entry
      keyed by the pin? Paths to check: training, symbolic search, backtest, pods
      snapshot, `load`, candidate diagnostics.
    - Can a worker still introduce an input artifact?
    - Is paper evidence always the candidate's own coordinator backtest?
    - Is the dry run's report of what it checks now accurate?
15. **Pre-review repairs (#32, #33).** Several checks:
    - **#32:** can a skew observation still block a cancel? Can any non-2xx write answer
      other than 400/401/403/422 be read as "not placed"?
    - **#33:** can the outcome join still pair a Kalshi result with a mapping from another
      run? Can a malformed artifact still stop a tick? Can a case or alias variant of a
      production path pass the ops guard?
16. **Live settlements (#34).** Can a venue settlement be recorded while the ledger's
    holding, payout or orders on that market disagree with it? Can a record for a market
    this ledger never traded, or a second record for a settled market, change the
    ledger? Can the settlements cursor advance past a record that was not recorded? Can
    a fill after a recorded settlement change cash? (`tests/trading/kalshi/test_kalshi_settlements.py`)

Commands:

```sh
cd <worktree>; .venv/bin/python -m pytest -q tests/forecast_storage tests/forecast \
  tests/evaluation tests/test_eligibility_rule_v2.py tests/history tests/datasets
.venv/bin/python tests/integration/rule_v2_archive_recheck.py --db <any collection archive>
```

## Codex Secondary — CLI, packaging, operations, recovery, bounded work

1. Wheel: `uv build`; install outside the checkout; `tests/integration/wheel_smoke.py
   --python <venv>/bin/python` (schemas 1–5, contracts 1–3, draft legacy archive migrated).
2. Ops: schema-5 allowlist; shadow procedure (`docs/forward-shadow.md`) with
   `--record-contract 3`; the live rule-v2 shadow evidence (forward-shadow handoff).
3. History acquisition: `vader history acquire` bounds and resume; dedicated-archive
   refusal; history profile allowlist vs collector profile.
4. Datasets: `vader dataset build|inspect` determinism (same inputs → same dataset id);
   registry append-only triggers; artifact write-once.

5. Trading CLI: `vader ledger ...` never orders; `vader live readiness` never loads the
   key; `vader live dry-run` writes only a paper ledger and has no HTTP client.
6. Pods: `vader pods ...` end to end (`tests/pods/test_research_pods.py::test_cli_*`);
   kill -9 of worker and coordinator (`tests/integration/test_recovery_demo.py`); budgets;
   no orphan processes; scrubbed environment; declared search budgets.
7. Demos: `.venv/bin/python tests/integration/e2e_demo.py` and `recovery_demo.py` run
   offline in about 20 s each; `real_data_demo.py` and `candle_vs_book_check.py` need the
   research data directories (read-only for production). `real_data_demo.py backtest
   --coordinator single --jobs N` runs one ledger per candidate and depth as parallel
   processes (the 2026 full grid took 1 h 40 min).
8. Ops: optional `model_registry` in the shadow block (validated against forbidden paths).

## Interfaces (both reviewers)

- `collector → discovery → witness → evidence.PROOF → tick → evaluation` (D-001/D-002).
- `history.acquire → history.interpret → datasets.history → research.registry`.
- `collection archive → datasets.collection` (lineage filter) ↔ `history archive` labels.
- `registry` → `pods.snapshot` → workers → `pods.evaluator` (recompute) → `candidates`.
- `registry model` → `research.backtest` (candles) → `trading.simulator`.
- `forecast decision (contract 4)` → `trading.shadow_dryrun` → `risk` → `executor` →
  `DryRunGateway` | (after activation only) `KalshiGateway`.
