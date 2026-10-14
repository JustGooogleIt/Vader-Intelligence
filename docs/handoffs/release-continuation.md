# Release continuation note (F3.3 repairs → F3.5 → research runner → forward shadow)

Durable checkpoint for the research-pipeline release. Updated at every milestone
boundary. Resume from here; do not reconstruct the analysis.

## Where the work lives

| Item | Value |
|---|---|
| Worktree | `/Users/ashwin/Documents/github-repos/Vader-Intelligence-release-work` (attached to the `-claude` checkout; own `.venv` from `uv sync --locked`, uv 0.12.19 at `../Vader-Intelligence/.venv/bin/uv`, Python 3.11.6) |
| Current branch | `feat/f35-evaluation` (on `feat/f34-completion` @ `23bd602` PR #13, on `fix/f33-foundation-repairs` @ `bd02c88` PR #12) |
| Base | `feat/f34-tick-selection` @ `5811991` (PR #11 head), on `feat/f33-record-contract-2` @ `841af2a` (PR #10), on `feat/f33a-discovery-boundary` @ `8f9f2c0` (PR #9), on `docs/f33-implementation-plan` @ `deea149` (PR #8), on `main` @ `ebb58df` |
| Review input | Independent PR #10 review (2026-10-12/14), report at `/Users/ashwin/Documents/Vader-Intelligence-reviews/20261012-pr10-evidence/REVIEW.md`, six defects P1/P1/P2/P2/P2/P2; no GitHub comments were posted by that review |
| Skills | Cool Coder 1.2.0 pinned at `e46e79805be0ee5877fa9bc993492064bbb40aa5` under `~/.codex/skills`; untouched |
| Production | Pinned old code on a schema-1 archive; never touched by this work. No merges, deploys, orders, credentials or paid services |

## Milestone ledger

| Milestone | State | Commit(s) | Notes |
|---|---|---|---|
| M0 Foundation repairs (six review defects) | **done, tested** | `27a3473` | All six reproduced on the PR #11 head before the fix (14 failing adversarial expectations, 2 positive controls passing); after the fix the ported regression file passes (20 tests) and the full suite is 749 passed / 2 pre-existing observer failures |
| M1 Durable forecast path (tick completion) | **done, tested** | `511f3c6` | Renewal at C, vetoes, later publication, reconstruct; see `docs/handoffs/f34-completion.md`. 767 passed / 2 pre-existing |
| M2 F3.5 evaluation materialization | **done, tested** | `ab09f72` | Outcome join, two views, immutable superseding runs; `docs/handoffs/f35-evaluation.md`. 789 passed / 2 pre-existing |
| M3 Research runner | pending | — | |
| M4 Reports | pending | — | |
| M5 Forward-shadow workflow | pending | — | |
| M6 Hardening | pending | — | |

## Test commands and last results (at `27a3473`)

```sh
cd /Users/ashwin/Documents/github-repos/Vader-Intelligence-release-work
.venv/bin/ruff check src tests ops            # passed
.venv/bin/ruff format --check src tests ops    # passed
.venv/bin/python -m pytest -q -p no:cacheprovider
#   749 passed, 2 failed (tests/worker_observer: missing optional tmux_jev; pre-existing on main/#9/#10/#11)
.venv/bin/python -m unittest discover -s tests/ops -q   # 31 run, OK
git diff --check                               # clean
# Reviewer's reproductions (file outside git):
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python -m pytest --noconftest -q \
  /Users/ashwin/Documents/Vader-Intelligence-reviews/20261012-pr10-evidence/test_independent.py
#   on 5811991: 14 failed, 2 passed (defects confirmed)
#   on 27a3473: the five remaining "failures" are in that file's own setup (its helper
#   now needs a witness run) and in the supplied-receipt case, which is now rejected at
#   insert; the ported regression file tests/forecast_storage/test_review_defects.py is
#   the maintained form of those checks (20 passed).
```

## Design decisions made in M0 (so a reviewer can disagree)

1. **Lineage is persisted, not inferred from the clock.** `Collector.run` records
   `tool_configuration.source_lineage` (`live` | `synthetic`) in the run provenance;
   the witness and tick runs record their own. `references.run_source_mode` reads it;
   a fixture run is always synthetic; a two-phase run written before this field existed
   is treated as synthetic when its own immutable discovery pass says so (the inference
   can only narrow, never promote). `RunProvenanceV1` is unchanged (additive key in the
   free `tool_configuration` dict). A resume refuses a different lineage.
2. **Contract 2 for receipts now means "observed by the witness".** The journal requires
   a `forecast-witness` run whose provenance names the receipt's `observer_namespace`
   and `session_id`. Contract 2 has never shipped (PR #10 is an unmerged draft; no
   retained archive is at schema 4), so the rule is amended in place rather than minting
   contract 3 / schema 5. Flagged for Codex Prime: if any schema-4 archive outside the
   disposable test set is believed to exist, this becomes a contract-3 decision.
   Supplied assertions remain what contract 1 always was: stored, valid, never proof;
   contract 1 stays closed to new receipts on schema 4 as the plan decided.
3. **Cheap contract preflight before mutation.** `schema.check_contracts` scans only the
   payload `contract_version` field of the research tables (no reference re-hashing)
   and runs on the real connection before WAL on every open and before every
   migration. Cost is linear in research rows; measured in M6.
4. **Scores need provenance.** An evaluation item score requires the decision's stored
   probability for that baseline and a cited discovery pass (so the collector cohort
   is known); unpinned decisions are counted (`unpinned_decisions`) and stay unscored.
5. **Witness skips what it cannot legally witness** (historical-reconstruction passes
   and bindings, live subjects under an injected clock) and reports counts; a subject
   the journal refuses is reported in `errors`, skipped, and the run finishes `partial`.
   `IdempotencyConflict` still fails the run (two observations for one subject).
6. **Per-invocation caps**: `max_passes` for passes, `max_passes` for research subjects
   (bindings + publications), `max_subjects` for fetches, cumulative across batches;
   `remaining_by_kind` reports eligible work left; reaching a cap with work left is
   `partial`.

## F3.4 gaps closed in M1

All items listed by the M0 reading were closed in `511f3c6` except the schedule-revision
state machine, which stays deferred with an explicit `schedule_revised` abstention
(`docs/handoffs/f34-completion.md`).

## Unresolved semantic questions (decisions needed, work continues around them)

- Q-M0-1: contract-2 amendment in place vs contract 3 (decision 2 above).
- Q-M2-1 (decided, flagged): a binary label requires a `mapped` settlement mapping that
  agrees with the frozen gamePk; without one the item is `identity_unresolved`.
- Q-M2-2 (decided, flagged): contract 2 receipts/decisions and the evaluation layer treat
  `status=complete` as "paired scored N > 0", else `inconclusive` (§7 exit codes).
- Q-M1-1 (decided, flagged): a later tick publishes an eligible decision that lacks a
  publication record, at the actual later time (allowed in window, missed after);
  alternative was to leave `not_attempted` forever.

## Next action

M3+M4 on `feat/research-runner` (stacked on `feat/f35-evaluation`): the offline
research runner (`vader research run`) over the existing reconstruct/evaluate
interfaces with an immutable run manifest in `runs`, explicit split boundaries, one
evaluation per collector cohort, resume by construction; machine-readable and text
reports plus `evaluation compare` on identical populations; the deterministic demo
script with expected results.
