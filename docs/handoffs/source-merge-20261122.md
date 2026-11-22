# Source merge of the research stack (2026-11-22)

On 2026-11-22 the owner instructed Claude to keep making progress and to merge the
pull requests. This records exactly what was merged, how it was verified, and what is
still open. It is a **source merge only**: nothing was deployed, no production
checkout, configuration, archive or service was touched, no archive was migrated, the
live adapter was not activated, no provider or paid call was made, and no holdout was
consumed.

## What merged

| Step | PR | Head merged | Merge commit on `main` |
|---|---|---|---|
| 1 | #38 integration checkpoint (contains every commit of #8–#37) | `f5e92b75da9567665fe84be28e35eb0b48a17f51` | `011e0d8bf45a1977e26712c623df90f523bfb645` |
| 2 | #39 release validation (N-34a, F51, acknowledgement scope) | `ad94063c37b56e9ecf8f61b31175b770bcf5e6fc` | `a10bd92a47e3e28baaeab893c1eeb444fa9fc976` |
| 3 | #40 Command Center v1 | `ec4edeb97a6521ba5c07656f852f42a01fe6a7cb` | `a6b5dc06dc923b0c755f0af42f8f481bc41ef867` |
| 4 | #41 D-001 note on the F3.3 plan; N-CC-1 lint fix | `94fe87a` | `093cfd3e3513f4c7ea7d37485e594999b07059c9` |

`main` before the merge: `ebb58df9b89922db4828fda7d23891dd917a4b90`. All merges are
ordinary merge commits; no history was rewritten and no force-push was used.

- #8 was marked merged by GitHub when #38 merged (its base was `main`).
- #9–#37 targeted stacked branches, so GitHub could not mark them merged. Each was closed
  with a comment recording that its exact head SHA is an ancestor of `main`. A script
  checked that ancestry for every PR before closing it. #25 had already been closed
  automatically by GitHub when #38 merged (a commit in the stack references it); it
  received the same record comment. No PR was rejected.
- Every author branch, recovery ref (`recovery/*`) and the `cc/*` writer branches are
  left in place.

### #40 merge commit created locally

After #39 merged, GitHub reported #40 as conflicting, and then "cannot be cleanly
created" even after its base was refreshed. #40's history has two merge bases with
`main` (`f5e92b7` and `ad94063`). Git's own merge of `origin/main` with `ec4edeb` is
clean, and its tree is byte-identical to `ec4edeb` (`git diff --quiet` against the
tested head). That merge commit was created locally and pushed as a fast-forward of
`main`; GitHub then marked #40 merged at `a6b5dc0`.

## Verification before merging

All runs are this session's own, on macOS (arm64), Python 3.11.6, uv 0.12.19, in a fresh
worktree of the exact #40 head `ec4edeb` (which contains #38 and #39) with
`uv sync --locked`.

| Check | Result |
|---|---|
| Full suite, `python -m pytest -q` | **1568 passed, 2 failed, 29 subtests passed** in 356 s. The two failures are the known baseline: both import `tmux_jev`, which is not in the core environment (`test_upstream_pin_guard_and_cli_help`, `test_bridge_assessment_uses_frozen_input_with_mock_provider`). #40's own record shows the same 1568 / 2 |
| Observer suite, existing pinned environment (`tmux-jev` at `167f94359728a6356fb8a76e62f5cfdc16a6d881`, verified from its `direct_url.json`; nothing installed) | **85 passed** |
| `ruff format --check src tests ops` | 267 files already formatted |
| `ruff check src tests ops` | 4 I001 errors, all N-CC-1; fixed by #41, after which all checks pass |
| `uv build`, fresh environment, `tests/integration/wheel_smoke.py` | status `complete`, **54 checks**, synthetic |
| After #41, on `main` `093cfd3` | `ruff check` clean; `tests/datasets tests/history tests/integration` 61 passed |

Not re-run here: the earlier Linux and Windows reviews, Codex's runs, and any production
or live check.

## What the merge does not decide

Merging puts code on `main`. It does not deploy it, and it does not accept any open policy
decision. Every opt-in stays opt-in and every default stays as it was:

| Item | State after the merge |
|---|---|
| #8 plan revision 3 | The recommended D-001 note was added (#41); the owner instructed the merge |
| D-001 | Accepted by the owner on 2026-10-19 (unchanged) |
| Q-M1-1, D-002, D-003, D-004 | Open owner decisions; the decision table in `claude-release-validation.md` recommends accepting each as is. Code paths for D-002 (rule v2) remain opt-in |
| D-005 NBA/NFL | Blocked on an authorized official schedule source |
| D-015 `last_trade_gap_c` | Open: drop the feature or compute it from a witnessed trade time, as `fs-market-v2` |
| D-016 | `selection-v1` stays the default; `selection-v2` opt-in |
| F50 | Recovered fallback `37c7bac` not applied; adopting it would be `evaluation-v3` |
| N-20c | Current tier order kept; a change needs `history-v2` |
| F34 holdout | One slot per model, either kind; the 2026 holdout is untouched |
| Live adapter | Disabled by default; never activated |
| Command Center real agents | Disabled; no authorization record of $0-incremental usage; maintenance checks are not sandboxed |

## Production

The production collector remains the older deployed code on a schema-1 archive. The
October observation was that it reads successfully but books nothing, because its rule
parser rejects every current candidate. The documented fix is the rule-v2 rollout in
`claude-release-validation.md`, "Production rule-v2 rollout". Step 1 of it, the source
merge, is now done. Steps 2–5 change production (backup, operator window, collector
configuration, reinstall, health watch) and need the owner. The MLB season has ended,
so a bounded live shadow would currently be inconclusive.

## Environments used

- Verification worktree: `/Users/ashwin/Documents/github-repos/Vader-Intelligence-claude-merge-verify`
  (private `.venv`, detached).
- Build output and wheel environment: `../claude-verify-dist`,
  `../claude-verify-wheel-env`; suite log `../claude-verify-40-pytest.log`.
- The observer environment of `Vader-Intelligence-observer-mac` was used read-only.
