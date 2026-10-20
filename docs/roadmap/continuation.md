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
| Pinned run worktrees | `-live-v2` (detached `622caad`, live rule-v2 shadow driver), `-live-models` (model shadow driver), `-history-run` / `-history-run2` (pinned acquisition code), `-verify` (now on `fix/activation-key-material`, #35) |
| Finished subagent worktrees (cleanup) | `-wt-deps`, `-wt-execution-risk`, `-wt-symbolic-engine`, `-wt-research-pods-core`, `-wt-models-library`, `-wt-live-adapter` |
| Research data (outside the repo) | `/Users/ashwin/Documents/Vader-Intelligence-research/{history,registry,pods-2026,pods-2025-2026,demo}` |
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
| #29 | `perf/ledger-row-cache` | D-012 ledger parsed-row reuse (5× less CPU per paper run); read-only registry for non-holdout backtests |
| #30 | `feat/pod-autopilot` | D-013 pod autopilot: development-only planners and a bounded loop of approved operations |
| #31 | `fix/review-26-27` | repairs from the independent #26/#27 pre-review: D-014 pinned holdout, coordinator-only inputs, candidate-bound evidence, dry-run fidelity |
| #32 | `fix/review-24-25` | repairs from the #24/#25 pre-review: live-adapter hardening (skew, TLS, writes, exits, bindings), ingest idempotency, fee bound, Kelly rounding |
| #33 | `fix/review-17-23-28` | repairs from the #17–#23 and #28 pre-review: stale settlement mapping, forecast-tick abstention, synthetic label, case-insensitive ops guard |
| #34 | `feat/live-settlements` | F live settlement recording: venue settlements recorded only on an exact match with the ledger |
| #35 | `fix/activation-key-material` | repair: the activation gate refuses encoded key material (also merged or misaligned runs); runbook note on canary risk limits within the notional caps |
| #36 | `fix/book-selection-witnessed` | repair (D-016): opt-in `selection-v2` selects the midpoint's book as spec §4 says; default unchanged |

Full SHAs are in the final handoff (`docs/handoffs/roadmap-handoff.md`).

## Background processes (bounded; they end by themselves)

| What | Where | Ends |
|---|---|---|
| Live forward-shadow driver (rule v2, test label, no launchd) | `/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-rule-v2/driver.sh`; decision at C = 23:08:00Z for `KXMLBGAME-26OCT192008CLETB`, `shadow-evaluate` at 05:00 UTC 2026-10-20 | ≈05:05 UTC 2026-10-20 (decision recorded at 23:08:12Z) |
| Model shadow driver (three 2026 pod models as registered forecasters) | `/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-models/driver.sh`; evaluation 05:15 UTC | ≈05:20 UTC 2026-10-20 (decision recorded at 23:08:12Z) |

The acquisitions are complete (2025 date-only identities; 2026 execution windows, 4,810
post-cutoff windows, none failed). The real-candle backtests are recorded in `docs/evidence/real-data-demo-20261019.md`
§3 (2026) and §3b (two seasons); the autopilot is in §4.

## Next action

Done so far:
- The real-candle backtests (§3, §3b) and the autopilot (§4) are recorded.
- Every PR has had an independent read-only pre-review, with repairs in #30–#33 and the
  open items in the handoff (§3b, §6, §8).
- 2026-10-19, live: both shadows decided at C = 23:08Z, and the publications were
  allowed. The three model probabilities were 0.461, 0.438 and 0.413, against a market
  mid of 0.415.
  - The midpoint abstained in both shadows (`book_availability_after_cutoff`). An
    independent read-only review traced this to `select_inputs` departing from spec §4.
    It is D-016, repaired as an opt-in `selection-v2` in #36
    (`fix/book-selection-witnessed`).
  - The dry runs on the real decision are recorded. The logistic model would buy 8 YES
    at 0.42; the 1.00 cap refuses that order. #35 gained the runbook note on canary
    limits (`67b260a`, with caveats in `78eea4d`).
  - #35's key-material test failed intermittently, about one run in three. `e9f4b89`
    fixes the detector for runs merged with neighbouring text or misaligned. An
    adversarial review then found that a body pasted as TOML comments passed every
    gate. `5b7397e` fixes that, plus arrays, truncated and OpenSSH bodies, and slow RSA
    validation. #35 is now at `5b7397e`, with 1361 passed.
  - #36 is at `6a2bc50` (1379 passed), which merges #35's hardening; it was not rebased.
    Its read-only pre-review found no correctness defect, and its test gaps are closed.
  - Evidence: `docs/evidence/live-forward-20261019.md` and `.json`. The JSON is
    regenerated by
    `/private/tmp/claude-501/-Users-ashwin/cc9d4483-1bdc-4f4f-a532-a2811c7f035f/scratchpad/live-evidence/extract.py`
    (read-only).

Next:

1. **After 05:00 UTC (rule v2) and 05:20 UTC (models) on 2026-10-20:** read both
   `shadow-evaluate.json` reports. Re-run the extract, and add the settlement and
   evaluation results to the evidence file. The fs-market-v1 models carry the D-015
   caveat, and the stored midpoint abstained under selection-v1 (D-016).
   - If the game has not settled by then, run the same `manage.py shadow-evaluate`
     command once more after the drivers finish.
2. **Final handoff:**
   - verify every PR's head SHA against GitHub;
   - write §11 (the head of #28) and add the live results;
   - update the matrix, backlog and evidence index states;
   - update the PR #28 description: "after fees" wording, #29–#36, D-014 to D-016,
     live results, and the suite at the final head.
