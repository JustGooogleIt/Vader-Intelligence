# Claude session handoff, 2026-11-22/23

Owner instructions this session: keep making progress and merge PRs; prepare the
production rule-v2 rollout; choose the NBA/NFL sources after ranking them; make the
D-015 call; adopt F50; enable and launch Claude coding agents; accept Q-M1-1 and
D-002–D-004.

## Merged to `main`

| PR | What |
|---|---|
| #38, #39, #40, #41, #42, #43 | Source merge of the whole stack (#8–#40), D-001 note, N-CC-1 lint fix, merge record (`source-merge-20261122.md`) |
| #44 | Owner decisions: Q-M1-1, D-002, D-003, D-004 accepted |
| #45 | F50 fallback as `evaluation-v3` (D-017); `compare` refuses mixed evaluator versions |
| #46 | Command Center runs authorized Claude coding agents (D-018) |
| #47 | `last_trade_gap_c@2`, `fs-market-v2` / `fs-full-v2` (D-019, resolves D-015) |
| #48 | NFL/NBA source decision (D-020) and plan `docs/specs/nfl-nba-v1-plan.md` |
| #49 | NFL/NBA pure identity, schedule parsing and eligibility (A5 increment 1) |

Every code PR was verified natively before merging (full suite: only the two known
`tmux_jev` baseline failures; ruff clean).

## Blocked on the owner

1. **Claude CLI sign-in.** The first real agent run (task: `settlement_evidence` cannot
   tell two unparseable records apart) failed closed with `agent_failed`: the standalone
   `claude` CLI is not signed in ("OAuth session expired"). Run `claude auth login` once in
   a terminal, then resubmit the brief at
   `~/Documents/Vader-Intelligence-command-center/brief-settlement-excerpt.json`.
2. **Disk space.** About 2.5 GB free (100% used). The production rule-v2 rollout needs a
   backup of a ~2 GB archive and cannot run safely until space is freed. Runbook:
   `production-rule-v2-rollout.md`.
3. **Codex and other agents.** Disabled until the owner supplies keys and an
   authorization record names them.

## Next engineering steps (no owner input needed)

- A5 increment 2 (plan §5): allowlisted GitHub release download with one bounded
  redirect, per-stage size caps, conditional GET, bounded gzip.
- A5 increments 3–5: per-league collection, forecast tick and evaluation, then a bounded
  NFL forward shadow on an isolated archive (disk permitting).
- Retrain research models on `fs-market-v2` before further model work.

## Command Center (development use)

Root and config: `~/Documents/Vader-Intelligence-command-center/` (`command-center.json`,
`root/authorization.json` naming `claude_cli` with a $10 per-run cap; delete the file to
disable agents). Start from a checkout with a synced environment:

```sh
cd ~/Documents/Vader-Intelligence-command-center
<checkout>/.venv/bin/python -m vader_intelligence.command_center serve --config command-center.json
<checkout>/.venv/bin/python -m vader_intelligence.command_center controller --config command-center.json --deadline-seconds 7000
```

Agents branch from the worktree named in `repo` (keep it at `main`). Nothing an agent
writes merges by itself; each run ends with a review packet and a recommendation.
