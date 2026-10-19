# Roadmap run continuation note

Durable checkpoint for the research-to-trading roadmap run (started 2026-10-18). Resume
from here; the backlog (`backlog.md`), decision log (`decisions.md`) and evidence index
(`evidence-index.md`) hold the detail. This note always names the exact next action.

## Where the work lives

| Item | Value |
|---|---|
| Main worktree | `/Users/ashwin/Documents/github-repos/Vader-Intelligence-roadmap` (attached to the `-claude` checkout; own `.venv`, `uv sync --locked`, uv 0.12.19 at `../Vader-Intelligence/.venv/bin/uv`, Python 3.11.6) |
| Stack base | PR #16 `feat/forward-shadow-ops` @ `82665cd02a9db9694f957eca32537cca5cf71c31` (itself on #15 → #14 → #13 → #12 → #11 → #10 → #9 → #8 → `main` `ebb58df`) |
| Parallel worktrees | `Vader-Intelligence-wt-execution-risk` (`feat/execution-risk`), `-wt-symbolic-engine` (`feat/symbolic-engine`), `-wt-research-pods-core` (`feat/research-pods-core`), all branched from `2d6a98f` (`wip/deps`: docs + dependency commit) |
| Production | Read-only queries only (`?mode=ro`); disk has ~6 GiB free, so no full copy of the 1.5 GB archive. No merges, deploys, orders, credentials or paid services |
| Baseline | 816 passed / 2 pre-existing `tmux_jev` failures at `82665cd` (83 s) |

## PR ledger

| PR | Branch | Scope | State |
|---|---|---|---|
| (next) | `fix/record-contract-3` | D-001 explicit contract versioning; legacy archives from older code | ready to open |

## Next action

Open the `fix/record-contract-3` draft PR against `feat/forward-shadow-ops`, then start A2
(eligibility rule v2, D-002) on a new branch stacked on it.
