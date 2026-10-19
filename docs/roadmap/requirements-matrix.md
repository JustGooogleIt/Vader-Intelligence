# Release requirement matrix (roadmap run 2026-10-18/19)

Status vocabulary: **implemented** (code + offline tests), **demonstrated** (run with a
recorded output), **simulated** (only against synthetic data or doubles), **untested**,
**blocked** (named external dependency), **not done**. Evidence paths are in
`evidence-index.md`; decisions in `decisions.md`.

| # | Requirement | PR | Status | Evidence / limitation |
|---|---|---|---|---|
| A1 | Contract-2 versioning: explicit record contracts, legacy archives open and classify | #17 | implemented, demonstrated on archives written by old code | D-001; `tests/forecast_storage/test_contract_versioning.py` |
| A2 | Postseason start-time eligibility | #18 | implemented; demonstrated on the real archive (60 rule pairs) and live (1 game, 2 eligible contracts on 2026-10-19) | D-002; forward decision at C 23:08Z 2026-10-19 pending |
| A3 | Schedule revisions | #19 | implemented | D-003; post-C changes unscored |
| A4 | Historical acquisition (H lineage, never forward) | #20 | implemented, demonstrated (2025 + 2026 seasons, 2026 execution windows) | 2025 identities are date-only (`start_corroborated = false`) |
| A5 | NBA/NFL if feasible | — | **blocked** | D-005: no authorized official free schedule source |
| B1 | Point-in-time feature definitions | #21 | implemented | leakage tests; forward-capable `fs-market-v1` |
| B2 | Datasets, manifests, chronological embargoed splits | #21 | implemented, demonstrated (2026 dataset, 2,012 dev+val rows + holdout) | history + collection sources |
| B3 | Baselines, regularized logistic, nonlinear tabular, calibration | #21 | implemented, demonstrated on real data | no model beats market mid on validation (selection-biased) |
| B4 | Registry, deterministic inference, compatibility checks | #21, #22 | implemented; forward run with registered models pending tonight | JSON artifacts, numpy inference, abstention on incompatibility (D-004) |
| C | Bounded symbolic regression | #23 | implemented, demonstrated on real data | best expression on 2026 data = `pair_mid_c` (the market) |
| D | Credible paper execution | #24, #26 | implemented; simulated (synthetic); demonstrated as a simulation on real 2026 validation candles (pods and coordinator agree exactly) | candles have no depth (D-010): fills only under assumed depth, and every filled scenario loses money; one-tick slippage means no fill with take-at-ask limits; quadratic run time |
| E | Risk management and fractional Kelly | #24 | implemented, simulated | hand-verified Kelly cases; authoritative risk engine (D-006) |
| F | Live Kalshi adapter, disabled by default | #25 | implemented, simulated against a deterministic double; **never activated** | D-007; canary runbook not executed; fee facts unverified |
| G1 | Durable orchestration (leases, retries, budgets, recovery) | #27 | implemented, demonstrated (recovery demo) | not a security sandbox; approved operations only |
| G2 | Research pods and candidate lifecycle with protected evaluator | #27 | implemented, demonstrated on real 2026 data (3 candidates rejected) and synthetic (full chain to paper_approved) | D-008; no live state |
| G3 | Autonomous planning (pod autopilot) | #30 | implemented, demonstrated on real 2026 data (heuristic planner: 8 tasks, 2 candidates rejected) and synthetic | D-013; development-only context; provider planner only against the scripted double (paid calls disabled) |
| H1 | Deterministic E2E demo with hand-checkable numbers | #28 | demonstrated, asserted in the suite | `tests/integration/test_e2e_demo.py` |
| H2 | Real-data demo | #28 | demonstrated (pods, evaluator, candle-vs-book, 2026 real-candle backtest); two-season backtest running | `docs/evidence/real-data-demo-20261019.md` |
| H3 | Recovery demos | #28 | demonstrated, asserted in the suite | `tests/integration/test_recovery_demo.py` |
| H4 | Live-adapter dry-run path | #26, #28 | implemented, simulated (synthetic forward decision); real forward decision tonight | D-011; never sends |
| H5 | One-time holdout evaluation of the final candidate | — | **not done by decision** | D-009: no candidate validated; holdout untouched; path demonstrated on synthetic data |
| X1 | No merges, deployment, production mutation, orders, paid calls, secrets | all | held | production read-only queries only; no credentials exist |
| X2 | Stacked draft PRs, no force-push under review | #17–#28 | held | the only rewrite was of unpushed branches |
| X3 | Persistent run documents | #28 | implemented | continuation, backlog, decisions, evidence index, review packets, this matrix, final handoff |
