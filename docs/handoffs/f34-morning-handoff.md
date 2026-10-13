# F3.3b-remainder + F3.4 handoff — 2026-10-12

Prepared by the implementing session in an isolated worktree of
`/Users/ashwin/Documents/github-repos/Vader-Intelligence-claude`, on this Mac, with its
own `uv sync --locked` environment (Python 3.11.6, uv 0.12.19). Nothing merged, deployed,
or run against production. All databases disposable; all evidence synthetic.

## 1. Draft PR, dependency order, base/head SHAs

Stacked, in review order. Nothing merged by this work.

| PR | Title | Base | Head SHA | Draft |
|---|---|---|---|---|
| [#8](https://github.com/JustGooogleIt/Vader-Intelligence/pull/8) | F3.3 implementation plan (docs only) | `main` | `deea149` | yes |
| [#9](https://github.com/JustGooogleIt/Vader-Intelligence/pull/9) | F3.3a discovery boundary | `docs/f33-implementation-plan` | `8f9f2c0` | yes |
| [#10](https://github.com/JustGooogleIt/Vader-Intelligence/pull/10) | F3.3b evidence: record contract 2, witness, projection | `feat/f33a-discovery-boundary` | `841af2a` | yes |
| [#11](https://github.com/JustGooogleIt/Vader-Intelligence/pull/11) | F3.3b-remainder + F3.4: tick, selection, decisions, publication | `feat/f33-record-contract-2` (#10 head `841af2a`) | code `6675232`; branch tip adds only this handoff | yes |

`main` is `ebb58df`. #11 is based on #10's branch so its diff is clean; retarget bases up
the stack as each merges. #9, #10 and #11 are all under or awaiting independent review.
#11 extends the witness additively (new `binding`/`publication` subjects) and otherwise
adds new modules, so it does not modify #9/#10's reviewed files.

## 2. Delivered requirements and remaining gaps

Delivered (tested on this Mac; trace in `docs/specs/f34-implementation-checklist.md`):

- Research protocol parser with a canonical hash distinct from the collector hash
  (`forecast/research.py`).
- Identity binding preparation, contract selection, book-input selection, decision
  generation and publication (`forecast/service.py`, `selection.py`, `publication.py`).
- The finite `forecast tick` and read-only `forecast inspect` (`forecast/tick.py`,
  `cli.py`); the witness now covers `binding` and `publication` subjects.
- A decision uses only evidence retrieved and witnessed by its cutoff, pins its discovery
  pass with the collector hash, keeps the research hash distinct, stays immutable, and is
  reproducible via `service.redecide`.

Remaining gaps (deferred, with handoff in `docs/handoffs/f34-tick-selection.md`):

- The schedule-revision state machine (`policy.decide_schedule`, moving C, multi-revision
  binding chains). This block prepares one binding revision per game; the decision key
  makes the first C sticky.
- `forecast reconstruct` (archive-only historical campaign). Its upstream contracts exist.
- F3.5 evaluation materialization — out of scope; its upstream contracts (contract-2
  decisions with the pin, publications, witnessed receipts, the cohort rule) are complete.
- One documented behaviour to confirm in review: a crash between a committed decision and
  its publication leaves `not_attempted`; a later tick does not retroactively publish it.

## 3. Runnable command sequence for the vertical slice (disposable data)

Offline, deterministic, on a throwaway database. Collection and the tick use the library
with injected time (the CLI `collect` needs live providers); migration, inspect and
replay use the real CLI. The whole slice is `tests/forecast/test_vertical_slice_f34.py`:

```sh
cd /Users/ashwin/Documents/github-repos/Vader-Intelligence-f34-work
.venv/bin/python -m pytest -q tests/forecast/test_vertical_slice_f34.py
```

The CLI commands the slice drives, on a schema-4 archive, are:

```sh
DB=/tmp/vader-slice/research.sqlite3; mkdir -p /tmp/vader-slice
.venv/bin/python -m vader_intelligence.cli --db "$DB" db-init
.venv/bin/python -m vader_intelligence.cli --db "$DB" settlement migrate
.venv/bin/python -m vader_intelligence.cli --db "$DB" forecast migrate
.venv/bin/python -m vader_intelligence.cli --db "$DB" forecast migrate --record-contract 2
# ... collection and the warm/due ticks run in-process with injected time ...
.venv/bin/python -m vader_intelligence.cli --db "$DB" forecast tick --budget 15   # live: inconclusive without forward evidence
.venv/bin/python -m vader_intelligence.cli --db "$DB" forecast inspect --limit 20
.venv/bin/python -m vader_intelligence.cli --db "$DB" replay
```

## 4. Test results and known failures

Run at **#11 code head `6675232`** (the branch tip adds only this handoff, no code or test
change), in the worktree with `.venv` from `uv sync --locked`:

```sh
.venv/bin/ruff check src tests ops                 # passed
.venv/bin/ruff format --check src tests ops         # passed
.venv/bin/python -m pytest -q                        # 729 passed, 2 failed
.venv/bin/python -m unittest discover -s tests/ops -q   # 31 run, 1 skipped (OK)
git diff --check                                     # clean
uv build && installed-wheel smoke                    # status complete, 22 checks
```

The **2 failures** are `tests/worker_observer/test_observer.py::test_upstream_pin_guard_and_cli_help`
and `::test_bridge_assessment_uses_frozen_input_with_mock_provider`. They belong to PR
#6's tmux worker observer, need an external tool (`tmux_jev`) absent in the locked
environment, and the `tools/worker_observer` and `tests/worker_observer` trees are
untouched by #9/#10/#11 (`git diff deea149 HEAD -- tests/worker_observer tools/worker_observer`
is empty). Baseline status under the same environment: they fail identically on the PR #8
base. They are pre-existing environment failures, not regressions. Everything else passes.

New forecast tests: `tests/forecast/test_research.py`, `test_service.py`, `test_tick.py`,
`test_vertical_slice_f34.py`, plus the extended witness/evidence/record-contract suites.
All ran natively on macOS; the schema-4 native tests that skip on Windows ran here.

## 5. Transaction, idempotency, cutoff and replay semantics

- **Transactions**: each record (binding, decision, publication, receipt) is one
  `BEGIN IMMEDIATE` journal transaction under the existing `storage.writer_lock`; no
  network call inside a transaction.
- **Idempotency**: binding key `(gamePk, policy_version, research config_hash, mode,
  revision)`; decision key `(dataset, mode, protocol, config_hash, gamePk, horizon)` —
  excludes time, ticker, schedule revision and snapshot, so repeats and concurrent ticks
  never create a second or retroactive forecast; publication is one per decision.
- **Cutoff**: selection admits only evidence retrieved at or before C and witnessed by a
  trusted contract-2 receipt observed at or before C; 300 s freshness via pure
  `policy.freshness`; HTTP Date read by nothing.
- **Replay**: `replay` preserves and reports `preserved_not_reconstructed`; `redecide`
  recomputes read-only from the same persisted inputs; appended later evidence never
  changes a historical decision (frozen at its own C).

## 6. Unresolved decisions

- Whether a later tick should publish a still-within-window decision that lacks a
  publication after a crash between the two records. Current behaviour: no (the decision
  is skipped as existing, publication stays `not_attempted`). Flagged for review.
- The schedule-revision state machine's exact binding-renewal interface
  (`decide_schedule`) is specified but not built; its design is the next F3.4 increment.

No semantic contract was invented to make progress; deferred items follow the spec and
are left to their named later steps.

## 7. Suggested independent review split for this block

- **Codex Prime — decisions, selection, cohort/provenance, replay.** `forecast/service.py`
  (binding identity, decision composition, the discovery pin and collector-vs-research
  hashes, `redecide`), `forecast/selection.py:select_inputs` (pre-C and witnessed-by-C
  filtering, no backward search), `forecast/publication.py`, and
  `tests/forecast/test_service.py` / `test_vertical_slice_f34.py`. Confirm no post-C
  evidence can enter a decision and that cohorts stay isolated.
- **Codex Secondary — tick orchestration, CLI, witness extension, concurrency/restart.**
  `forecast/tick.py` (the finite sequence, due/idempotency logic, exit codes),
  `forecast/witness.py` (binding/publication subjects), `forecast/research.py`, `cli.py`,
  `wheel_smoke`, and `tests/forecast/test_tick.py` / `test_research.py`. Confirm repeated
  and concurrent ticks and the crash/restart paths never duplicate or retroactively
  forecast, and the CLI exit codes match the spec.
- Both reviewers inspect the integration with #10: the tick witnesses and projects the
  evidence #10 defined, and pins the discovery pass #9 commits.

## Working tree

The worktree `/Users/ashwin/Documents/github-repos/Vader-Intelligence-f34-work` holds
`feat/f34-tick-selection`. It is a disposable git worktree attached to the `-claude`
checkout; remove with `git worktree remove` once fetched elsewhere. `dist/` and the wheel
venv were cleaned up and are gitignored.
