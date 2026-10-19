# Roadmap run continuation note

Durable checkpoint for the research-to-trading roadmap run (started 2026-10-18; user
asked for Opus 5.5 for this work, including subagents). Resume from here; the backlog
(`backlog.md`), decision log (`decisions.md`) and evidence index (`evidence-index.md`)
hold the detail. This note always names the exact next action.

## Where the work lives

| Item | Value |
|---|---|
| Main worktree | `/Users/ashwin/Documents/github-repos/Vader-Intelligence-roadmap` (attached to the `-claude` checkout; `uv sync --locked`, uv 0.12.19 at `../Vader-Intelligence/.venv/bin/uv`, Python 3.11.6) |
| Stack base | PR #16 `feat/forward-shadow-ops` @ `82665cd02a9db9694f957eca32537cca5cf71c31` |
| Parallel worktrees | `-wt-execution-risk` (`feat/execution-risk`, done: `479cf7f`, `ed67bc6`, `220aec3`), `-wt-symbolic-engine` (`feat/symbolic-engine`, done: `1d36973`, `f614594`, `b2f184d`), `-wt-research-pods-core` (`feat/research-pods-core`, done: `b0b3288`, `0feb693`, `3c0a79a`), `-wt-models-library` (`feat/models-library`, agent running), `-wt-live-adapter` (`feat/live-adapter` from `feat/execution-risk`, agent running); all originally from `wip/deps` `2d6a98f` |
| Pinned run worktrees | `-live-v2` (detached `622caad`, live shadow driver), `-history-run` (detached `f7d3263`, historical acquisition) |
| Production | Read-only queries only; disk ~7 GiB free; no full copy of the 1.5 GB archive |

## PR ledger (all drafts, none merged)

| PR | Branch | Head | Scope |
|---|---|---|---|
| #17 | `fix/record-contract-3` | `f8642bf` | D-001 contract 3 / schema 5, contract 2 frozen, legacy archives from old code |
| #18 | `feat/eligibility-rule-v2` | `622caad` | D-002 rule v2 (date-only postseason rules, ticker corroboration), mapping policy 2 |
| #19 | `feat/schedule-revisions` | `74a60a8` | D-003 schedule changes after C unscored; evaluator v2 |
| #20 | `feat/historical-acquisition` | `2f99f61` | A4 public historical acquisition (history profile, resumable) |
| (next) | `feat/datasets-models` | `549d073`+ | B features, datasets, registry, holdout ledger; models library and training to integrate |

## Background processes (bounded; they terminate by themselves)

| What | Where | Ends |
|---|---|---|
| Live forward-shadow driver, rule v2, test label, no launchd | `/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-rule-v2/driver.sh` (cycles 22:30:10–23:20 UTC on 2026-10-19 around C = 23:08:00Z of `KXMLBGAME-26OCT192008CLETB`, then `shadow-evaluate` at 05:00 UTC 2026-10-20) | ≈05:05 UTC 2026-10-20 |
| Historical acquisition 2025 then 2026 (default identity rules) | `/Users/ashwin/Documents/Vader-Intelligence-research/history/run-acquisition.sh` → `history.sqlite3` | ≈2 h after 05:10 UTC 2026-10-19 |

Follow-up acquisitions planned after it finishes (same archive, sequential): 2025 with
`--identity-rules default,history-date-only --post-cutoff-seconds 900`; 2026 with
`--no-pre-cutoff --post-cutoff-seconds 900` (execution-only windows).

## Next action

Integrate the models library (when its agent reports) into `feat/datasets-models`: training
pipeline (`models/train.py`) on registry datasets with chronological CV, OOF calibration,
validation and one-time holdout via the ledger; CLI `vader dataset build|inspect`,
`vader model train|evaluate`; then open the B PR. Then stack C (symbolic engine +
integration), D/E (execution-risk), F (live adapter), G (pods), H (integration).
