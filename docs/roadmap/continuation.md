# Roadmap run continuation note

Durable checkpoint for the research-to-trading roadmap run (started 2026-10-18; the user
asked for Opus 5.5 for this work, including subagents, never Fable). Resume from here;
the backlog (`backlog.md`), decision log (`decisions.md`), evidence index
(`evidence-index.md`) and review packets (`review-packets.md`) hold the detail. This note
always names the exact next action.

## Where the work lives

| Item | Value |
|---|---|
| Main worktree | `/Users/ashwin/Documents/github-repos/Vader-Intelligence-roadmap` (`uv sync --locked`, uv 0.12.19 at `../Vader-Intelligence/.venv/bin/uv`, Python 3.11.6) |
| Stack base | PR #16 `feat/forward-shadow-ops` @ `82665cd02a9db9694f957eca32537cca5cf71c31` |
| Pinned run worktrees | `-live-v2` (detached `622caad`, live shadow driver), `-history-run` / `-history-run2` (pinned acquisition code) |
| Finished subagent worktrees (cleanup) | `-wt-deps`, `-wt-execution-risk`, `-wt-symbolic-engine`, `-wt-research-pods-core`, `-wt-models-library`, `-wt-live-adapter` |
| Research data (outside the repo) | `/Users/ashwin/Documents/Vader-Intelligence-research/{history,registry,pods-2026,demo}` |
| Production | `~/Documents/Vader-Intelligence-operations/20260930-first-deployment/production.sqlite3`: read-only queries only; never copied (disk ~10 GiB free) |

## PR ledger (all drafts, none merged, none reviewed yet)

| PR | Branch | Scope |
|---|---|---|
| #17 | `fix/record-contract-3` | D-001 contract 3 / schema 5, contract 2 frozen, legacy archives |
| #18 | `feat/eligibility-rule-v2` | D-002 rule v2 for date-only postseason rules |
| #19 | `feat/schedule-revisions` | D-003 schedule changes after C unscored |
| #20 | `feat/historical-acquisition` | A4 public historical acquisition |
| #21 | `feat/datasets-models` | B features, datasets, registry, models, training |
| #22 | `feat/registered-forecasters` | D-004 decision contract 4, evaluation contract 2 |
| #23 | `feat/symbolic-search` | C bounded symbolic regression |
| #24 | `feat/paper-execution-risk` | D/E ledger, simulator, risk, Kelly |
| #25 | `feat/kalshi-live-adapter` | F live adapter, disabled by default |
| #26 | `feat/history-backtest` | D/H historical paper backtest, live-adapter dry run |
| #27 | `feat/research-pods` | G orchestration, real operations, protected evaluator |
| #28 | `feat/integration-demos` | H demos (E2E, recovery, real data, candle-vs-book) and run docs |

Full SHAs are in the final handoff (`docs/handoffs/roadmap-handoff.md`).

## Background processes (bounded; they end by themselves)

| What | Where | Ends |
|---|---|---|
| Live forward-shadow driver (rule v2, test label, no launchd) | `/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-rule-v2/driver.sh`; decision at C = 23:08:00Z for `KXMLBGAME-26OCT192008CLETB`, `shadow-evaluate` at 05:00 UTC 2026-10-20 | ≈05:05 UTC 2026-10-20 |
| Follow-up acquisition: 2025 (`default,history-date-only`, post windows) then 2026 post-only windows | `/Users/ashwin/Documents/Vader-Intelligence-research/history/run-acquisition-2.sh` → `history.sqlite3`; log `acquisition-log.jsonl` | after both runs (≤ 3 h each) |

## Next action

1. When the 2026 post-only acquisition finishes: run
   `tests/integration/real_data_demo.py backtest ...` (paper-backtest pods and
   coordinator backtests on real validation candles) and record the results in
   `docs/evidence/real-data-demo-20261019.md`.
2. When the 2025 acquisition is in: build the 2025 dataset (identity rules
   `default,history-date-only`; report the `start_uncorroborated` stratum separately),
   register it, and decide whether a two-season split changes the B/G conclusions.
3. After 05:05 UTC 2026-10-20: read `driver-log.jsonl`, `cycle-*.json` and
   `shadow-evaluate.json` in the shadow directory and record the live rule-v2 evidence.
4. Final handoff with every PR's full SHA, test results and cleanup list.
