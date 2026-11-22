# F3.3 overnight handoff — 2026-10-12

Prepared by the implementing session (Claude) in an isolated worktree of
`/Users/ashwin/Documents/github-repos/Vader-Intelligence-claude`, on this Mac, with its
own `uv sync --locked` environment (Python 3.11.6, uv 0.12.19). Nothing was merged,
deployed, or run against production. No production archive, service, launchd job or
schema was touched. All databases used were disposable.

## 1. Draft PRs, dependency order, base commits, final head SHAs

Stacked, in review order. Nothing merged by this work.

| PR | Title | Base | Head SHA | Draft |
|---|---|---|---|---|
| [#8](https://github.com/JustGooogleIt/Vader-Intelligence/pull/8) | F3.3 implementation plan | `main` | `deea149` | yes, **documentation only, left as is** |
| [#9](https://github.com/JustGooogleIt/Vader-Intelligence/pull/9) | F3.3a discovery boundary | `docs/f33-implementation-plan` (#8 head `deea149`) | `8f9f2c090cc941304d2e409be65a82b8b9559c59` | yes |
| [#10](https://github.com/JustGooogleIt/Vader-Intelligence/pull/10) | F3.3b evidence: record contract 2, witness, projection | `feat/f33a-discovery-boundary` (#9 head `8f9f2c0`) | code/tests verified at `0eb9119df1205bdfd9a54948995477752cf5740c`; branch tip adds only this handoff document | yes |

`main` is `ebb58df` (PRs #6 and #7 merged). #9 is based on #8's branch so its diff stays
clean; when #8 merges, retarget #9's base to `main`. #10 is based on #9. Both reviewers
should inspect the integration between #9 and #10: #9 writes the discovery passes that
#10 witnesses and projects.

Dependency note: #9 (F3.3a) needs nothing beyond `main` and the plan; it writes only
contract-1 discovery passes and works on schema 3. #10 (patch P plus the witness and
projection) needs #9 for the discovery-pass subjects and adds schema 4. They are
separate because #9 is affected by nothing #10 lands.

## 2. What works end to end, and what is not done

Works, proven by tests on this Mac:

- **Two-phase schema-3 collection (#9).** Discovery, one immutable `discovery_passes`
  summary, then books. Incomplete discovery commits an honest summary and requests no
  books. Enforced at the persistence boundary (`Store.request` → `DiscoveryGateError`),
  not only by the caller. Schemas 1 and 2 keep the reviewed interleaved loop
  byte-for-byte.
- **Record contract 2, migration 004, schema 4 (#10).** Versioned receipts and
  decisions coexist with frozen contract-1 rows; old binaries refuse schema 4 at open
  (proven against `main` extracted with `git archive`).
- **Witness (#10, library only).** Produces contract-2 receipts at the real observation
  time with trusted/untrusted clock status. No caller in this increment.
- **Availability projection (#10).** Feeds the merged F3.1 pure policy from stored rows.
- **Vertical slice (#10).** `tests/forecast/test_vertical_slice.py` drives collection →
  witness → projection → `policy.cutoff_eligibility` → baselines, with eligibility after
  the observation and abstention before it.
- **Cohort rules (#10).** One collector configuration hash per evaluation; no pooling; no
  contract-1/contract-2 mixing; research hash distinct from collector hash; 300 s
  freshness preserved.

Not done, deferred to their own plans (specification F3.3b remainder, F3.4, F3.5):
decision/publication generation from live evidence, the selection and publication
services, the research-configuration parser and `forecast tick` (the witness's eventual
caller), and evaluation materialization. Until the tick exists, no operational receipt is
produced by anything in the repository, and the projection reports live inputs as
`availability_unproven` — this is by design.

## 3. Test commands and results, tied to each final head

All run in the worktree with `.venv` from `uv sync --locked`.

At **#9 head `db64ddd`** (code) and `8f9f2c0` (with docs), re-run at the current head:

```sh
.venv/bin/ruff check src tests ops            # passed
.venv/bin/ruff format --check src tests ops    # passed
.venv/bin/python -m pytest -q tests/test_discovery_boundary.py   # 38 passed
.venv/bin/python -m unittest discover -s tests/ops -q            # 31 run, 1 skipped
```

At **#10 code head `0eb9119`** (the last commit that changes code or tests; the branch
tip is one further commit that adds only this handoff, so this verifies the final state
of the code, not an earlier version of it):

```sh
.venv/bin/ruff check src tests ops                               # passed
.venv/bin/ruff format --check src tests ops                      # passed
.venv/bin/python -m pytest -q                                    # 691 passed, 2 failed
.venv/bin/python -m unittest discover -s tests/ops -q            # 31 run, 1 skipped (OK)
git diff --check                                                 # clean
# installed-wheel smoke, from outside the source checkout:
uv build                                                         # wheel + sdist built
uv venv .venv-wheel-f33
uv pip install --python .venv-wheel-f33/bin/python dist/vader_intelligence-0.1.0-py3-none-any.whl
.venv/bin/python tests/integration/wheel_smoke.py --python "$PWD/.venv-wheel-f33/bin/python"
#   -> status complete, 19 checks (db-init, fixture replay, migrations, forecast
#      migrate, schema-4 backup/restore/replay, schema-5 unknown-version rejection)
```

The **2 failures** are `tests/worker_observer/test_observer.py::test_upstream_pin_guard_and_cli_help`
and `::test_bridge_assessment_uses_frozen_input_with_mock_provider`. They belong to PR
#6's tmux worker observer, need an external tool (`tmux_jev`) that is not in the locked
environment, and the `tools/worker_observer` and `tests/worker_observer` trees are
untouched by both PRs (empty `git diff deea149 HEAD -- tests/worker_observer tools/worker_observer`).
They are pre-existing environment failures, not regressions. Everything else passes.

Platform note: all tests ran natively on macOS (darwin, Python 3.11.6). The native
Store/CLI/lock/replay tests under `tests/forecast_storage` that skip on Windows ran here.
No result is claimed from a portable run only.

## 4. Migration and legacy-compatibility behavior

- `vader forecast migrate` is unchanged for 2→3 and is a no-op at 3 and 4. The new
  `vader forecast migrate --record-contract 2` performs migration 004 on a schema-3
  archive: it appends the ledger row and sets `user_version` 4 in one transaction,
  changing no table and touching no row. It is a no-op at 4 and refused below 3. Nothing
  ever downgrades.
- Contract 1 (no `contract_version`) stays valid and inspectable forever. On schema 4 it
  is closed to new receipts and decisions, but exact retries of existing contract-1
  envelopes still return their original id. No row is ever converted.
- A binary without PR #10 refuses a schema-4 archive at open and leaves its bytes
  unchanged (proven against `main` at `ebb58df`).
- Backup, restore and replay preserve a mixed schema-4 archive byte for byte, with
  per-contract counts, and `journal.integrity` fails closed on an unknown
  `contract_version` or a contract-2 row in a schema-3 archive, naming the row.
- Rollback: code-only to `main` while an archive is at schema 3 (every row #9 writes is a
  contract-1 pass `main` validates). After migration to schema 4 it is **not** code-only;
  fix forward or restore a pre-migration backup to a new path (plan §5.5).

## 5. Live and endurance checks

None run. No live provider calls were made; every test uses injected transports and
clocks, so all stored evidence carries synthetic lineage. No endurance harness was
started: F3.3a and F3.3b evidence are offline storage and library work with no runtime
gate, and the plan defines no endurance gate for them (the 24-hour and continuous-
collection gates belong to operations, out of scope here). The one synthetic performance
measurement (720 two-phase passes: ~6.3 KB/summary, ~4 ms/insert, 0.30 s integrity) is
in `docs/handoffs/discovery-boundary.md`; its script and raw output are in the session
scratchpad, outside the repository.

## 6. Remaining defects, unresolved decisions, gates not passed

- No open defects found in the delivered scope. Self-review of the full diff checked for
  legacy-semantics changes (none; the legacy loop is byte-identical and verified),
  mutation before validation (none; the Store gate and `journal._insert` validate before
  the single write), incomplete-discovery leakage (gated at persistence and tested),
  cohort pooling (rejected in `check_cohort` and tested), hidden clock assumptions (the
  witness's residual limits are stated in the handoff and plan §4.4), and claims
  unsupported by tests (the 1 MiB `manifest_too_large` fallback is implemented but not
  exercised, flagged as code-only in the checklist, item A-21).
- No unresolved semantic decision was invented. The plan's operator decisions Q1–Q3 are
  implemented as written (cohorting, HTTP `Date` diagnostics-only, no books after
  incomplete discovery). Deferred items are the F3.5 cohort storage, the valid-but-
  ineligible treatment of contract-1 bindings/publications, and the §9.4 alternative
  book rule — all left to later plans, not decided here.
- Gates not passed: no live forward demonstration (requires the F3.4 tick and is
  inconclusive by the spec until a real pre-cutoff opportunity occurs); no 24-hour or
  continuous-collection evidence. Both are out of F3.3 scope.

## 7. Proposed review split

- **Codex Prime — storage, versioning, migration, legacy semantics, evaluation/cohort
  correctness.** Focus on PR #10: `contracts.py` version dispatch and the discovery pin,
  `journal.py` contract-aware link validation, `require_contract_schema`, `check_cohort`
  and per-contract integrity counts, `schema.py` migration 004, and the preservation
  tests (`test_record_contract.py` N1–N12, `test_evidence.py`). Confirm no legacy row is
  relabeled and that cohorts never pool across collector hashes.
- **Codex Secondary — discovery completeness, collector failure paths, CLI/replay,
  macOS locking, packaging, operational regressions.** Focus on PR #9: `collector.py`
  two-phase path and the byte-identical legacy loop, `storage.py` discovery gate and
  health, `transport.py` `Reader.live`/`PaginationError`, `test_discovery_boundary.py`
  (including the real SIGKILL subprocess), and the installed-wheel smoke. Confirm the
  persistence gate cannot be bypassed and schemas 1/2 are untouched.
- Both reviewers inspect the #9↔#10 integration: the witness and projection in #10 consume
  the discovery passes #9 commits, and the vertical-slice test exercises both together.

## Working tree

The worktree at `/Users/ashwin/Documents/github-repos/Vader-Intelligence-f33-work` holds
both branches (currently on `feat/f33-record-contract-2`). It is a disposable git
worktree attached to the `-claude` checkout; it can be removed with
`git worktree remove` once the branches are fetched elsewhere. `dist/` and the wheel
venv were cleaned up and are gitignored.
