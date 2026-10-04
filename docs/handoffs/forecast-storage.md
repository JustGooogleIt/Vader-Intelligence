# F3.2 forecasting storage foundation

## Writable Store initialization follow-up (2026-10-03)

Started from `1ed4bf109c383ad04190efa7d1872cd4f3e7c740` on the isolated
`feat/forecast-storage` worktree. Prime confirmed the two original P2 fixes and
reported a later byte-preservation defect: writable Store enabled WAL before
rejecting a pre-fix schema-3 archive. The earlier rejection tests checked rows and
schema SQL while a fixture connection remained open; they did not prove preservation
of closed DELETE-mode archive bytes. Prime's native reproduction is reported
evidence, not a native test performed in this Windows session.

The narrow fix extracts the existing compatibility check into
`Store._validated_schema_version()` and calls it on **self.db**, before any setup
PRAGMA or initialization write. Only accepted versions/layouts proceed to WAL and
normal initialization. `migrate()` retains the same validation and explicit upgrade
semantics. All setup now sits inside the existing exception/connection-close path.
No separate path preflight/reopen is relied upon. Existing external writer-lock
ownership is unchanged: CLI paths continue to hold it around Store construction,
and the persistent `.lockfile` inode is intentionally retained on rejection.

New `test_store_initialization.py` has 15 native cases covering:

- Closed, quiescent populated pre-fix schema 3 and unknown future schema 4 in DELETE
  mode; direct Store opens (writable/read-only) and actual CLI subprocess rejection.
- Exact database bytes, DELETE journal mode, and absence of unexpected sidecars
  after repeated rejection. CLI may leave only its expected `.lockfile`; tests also
  verify that the lock was released.
- Fresh initialization and supported schemas 1/2/3, repeated opens, explicit CLI
  1→2→3 migration/no-ops, and validation of the actual connection if the open target
  differs from the named path. No POSIX locking/alarm shim is used.

Windows verification actually performed:

```powershell
.venv/Scripts/python.exe -m pytest --noconftest -q tests/forecast_storage tests/forecast tests/evaluation
.venv/Scripts/python.exe -m unittest discover -s tests/ops -q
.venv/Scripts/ruff.exe check src tests ops
.venv/Scripts/ruff.exe format --check src tests ops
../forecast-policies/.venv/Scripts/uv.exe build
git diff --check
```

**258 passed, 2 native modules skipped**; operations **31 run, 1 skipped** (30
passed). Lint/format/diff checks and wheel/source builds passed. The wheel's Store,
schema-3 SQL and correction-lineage source exactly match the worktree; the packaged
Store source compiles. This is packaging validation, **not native Store execution**.
A normal invocation of the new test file was attempted but failed at the existing
root conftest's `fcntl` import. The 15 new native cases have therefore **not run here**.

Prime's focused Mac command, from a disposable checkout of the new head after
`uv sync --locked` (existing pinned uv 0.12.19):

```sh
.venv/bin/python -m pytest -q tests/forecast_storage/test_store_initialization.py
```

Then run the complete native recheck/package commands in **Outstanding Mac
verification** below. In particular the new closed-archive tests must pass before
claiming native byte preservation. WITHOUT ROWID, synthetic lineage, supported
migration paths and rejection of the old draft schema-3 layout are unchanged.
No schema definition change in this follow-up. PR #6 is owned by Prime and was not
accessed or changed; production, its schema-1 archive, merge and F3.3 are untouched.
Applied pinned Cool Coder `implement`, `data-systems-design`, and `code-review` at
`e46e79805be0ee5877fa9bc993492064bbb40aa5`; shared installations unchanged.

## PR #7 P2 corrections (2026-10-03)

Started from Prime's reviewed head `8640a290d6bad00ff927cb7195e83919f5fb1042`
on `feat/forecast-storage` in its existing isolated worktree. PR #6 work remains
separate. Prime's local `pr7-review.md` was not available here and was not read;
both findings were reproduced from the supplied descriptions before source edits.
The two initial regression tests failed on the old implementation: an explicit
rowid REPLACE succeeded, and a historical correction of a synthetic decision was
accepted. A separate reproduction confirmed that old integrity inspection also
reported that correction archive as `verified`.

### Fixes and schema compatibility

- `evaluation_items` now uses its existing composite primary key as its only row
  identity (`WITHOUT ROWID`). Its hidden rowid previously offered a replacement
  route outside the logical-key trigger. `rowid`, `_rowid_` and `oid` insertion
  attempts now fail because no such columns exist. The other six tables use
  `id INTEGER PRIMARY KEY`; their physical identity aliases are already covered
  by the existing BEFORE INSERT guard. Tests exercise every table/alias with both
  INSERT OR REPLACE and REPLACE, under recursive_triggers OFF and ON, preserve all
  rows/references, and verify exact application retries. No connection setting is
  relied upon to stop implicit replacement deletions.
- Decision and evaluation-run supersedes chains are checked edge by edge in the
  shared persistence/integrity validator. Synthetic ancestry cannot become a
  non-synthetic correction, including through previously malformed intermediate
  rows. Evaluation corrections retain the existing same-mode rule. Same-mode
  chains/retries remain supported. Cycles fail closed; at most 1000 ancestors are
  visited per record so inspection cannot enter an unbounded lineage walk.

**This changes the draft schema-3 definition**, while keeping user_version=3.
Schema 3 has not been released/deployed; production remains schema 1 and was not
accessed. Existing schema-1/2 paths are unchanged. New explicit 2→3 migrations use
the corrected layout; repeated migration on that layout remains a no-op.

Pre-fix disposable schema-3 databases are **unsupported by this revision**. Version
3 alone is insufficient: schema validation checks the required WITHOUT ROWID layout.
Store initialization/read-only opens, journal writes, integrity, ops inspection,
backup/restore validation and repeated forecast migration reject the old layout
with `unsupported schema-3 layout`. Tests preserve the populated old database's
rows, references, schema SQL and version after rejection; no implicit rebuild or
schema repair occurs. The native Store/CLI rejection test still needs Mac execution.

Keep any pre-fix archive untouched, including its associated SQLite files; do not
drop/recreate its table, lower user_version, or overwrite it with a fresh archive.
For further disposable tests, create a **new path** from a preserved schema-2 backup
using SQLite backup/restore, then explicitly run forecast migrate, or create fresh
synthetic fixtures. This does not recover/reconstruct old forecasting rows: those
remain in the preserved old archive. No in-place upgrade or automatic transfer of
pre-fix research history is provided. If that history must be carried forward,
stop and design a separately reviewed explicit preservation/validation migration.
Current ops tooling intentionally refuses to certify a pre-fix schema-3 backup;
retaining the old archive is not a claim that its provenance was valid.

### Verification of these fixes

Windows / Python 3.12.14, private environments and existing pinned tooling:

```powershell
.venv/Scripts/python.exe -m pytest --noconftest -q tests/forecast_storage/test_p2_regressions.py
.venv/Scripts/python.exe -m pytest --noconftest -q tests/forecast_storage tests/forecast tests/evaluation
.venv/Scripts/python.exe -m unittest discover -s tests/ops -q
.venv/Scripts/ruff.exe check src tests ops
.venv/Scripts/ruff.exe format --check src tests ops
../forecast-policies/.venv/Scripts/uv.exe build
git diff --check
```

**54 new regression cases passed; 258 combined tests passed, 1 Unix module skipped.**
Operations: **31 run, 1 skipped** (30 passed). Lint/format/diff checks passed; wheel
and source distributions built. Installed the wheel with --no-deps in a new private
`.venv-wheel-f32-p2` and used isolated Python (`-I`) to create an in-memory synthetic
schema-2 database from the packaged SQL: 2→3 migration, WITHOUT ROWID verification,
repeated-migration no-op and integrity all passed from site-packages. This portable
package check does not exercise the Unix collector CLI or writer lock.

A normal invocation of `tests/forecast_storage/test_native_integration.py` was
attempted and blocked by the existing `fcntl` import in root conftest. No mocked
POSIX locks/alarm shims were used. The exact Mac recheck commands below remain
required at the new commit, including native Store/CLI rejection of the old layout,
restore/replay, real lock exclusion and installed-wheel CLI checks. Keep PR #7 draft
and unmerged pending Prime's re-review and those checks.

Applied pinned Cool Coder `implement`, `code-review`, `data-systems-design` at
`e46e79805be0ee5877fa9bc993492064bbb40aa5` (1.2.0); shared installations unchanged.
Review focus: H-03/14 SQL identity and retries, H-09 explicit draft compatibility,
H-38 preserved history, bounded lineage and rejection of synthetic promotion.
No F3.3, forecasting generation, production access or deployment.

## Original F3.2 implementation record

F3.1 PR #5 was merged with a merge commit after verifying the cleared head
`ae1769781b2c1efaac837c59e0d722cd375b9bf1`, clean mergeability and no required
checks/protection rules. Merge/resulting main:
`804873b841d45d9e4e3478dca9feba86fa36a6ea`. Its description attributes Primary's
reported 152 focused / 349 combined tests plus 10 subtests and native package/lint
checks to Primary, separately from this author's earlier Windows verification.
No tests were rerun merely to merge unchanged F3.1.

`feat/forecast-storage` starts at that exact main in a separate worktree. Read the
merged specification (§6–8), specification/F3.1 handoffs and repository baseline
instructions. Only migrations 001/002 existed; 003/schema 3 was free. No production
checkout, archive, configuration, service or PR #6 worker-observer files were used.
Repository visibility remains public.

## Storage contracts

Seven additive tables: `discovery_passes`, `forecast_bindings`, `forecast_receipts`,
`forecast_decisions`, `forecast_publications`, `evaluation_runs`, `evaluation_items`.
The corrected schema-3 layout requires `evaluation_items` to be WITHOUT ROWID;
the pre-fix layout compatibility restriction above applies throughout this handoff.
Each keeps a canonical, version-1 complete `payload_json` and SHA-256. SQL projects
only the indexed identity, uniqueness and FK fields; CHECK constraints bind those
projections to JSON. This avoids duplicating the full research manifest across
many scalar columns. Required envelope fields/types are defined in
`forecast/contracts.py`, with fully populated synthetic examples in
`tests/forecast_storage/fixtures.py`. Unknown/missing outer fields are rejected.

- `schema.migrate(db)` owns an explicit `BEGIN IMMEDIATE`; accepts 2→3 or validates
  an existing 3 and returns `False`. It refuses active transactions, disabled FKs,
  missing legacy tables, inconsistent migration history and unsupported versions.
  DDL, migration ledger insertion and user_version commit atomically. It does not
  execute an implicit-commit `executescript` inside an existing transaction.
- `journal.append(db, table, envelope)` stores one supplied discovery/binding/
  receipt/decision/publication record. It does **not** perform any of those future
  operational activities. The discovery builder, witness, selection and generator
  remain unimplemented. All writing runs must already exist, carry matching code
  identity and leave prompt/provider/model/settings null.
- `journal.append_evaluation(db, envelope, items)` persists a supplied complete
  report and its items atomically. Items omit evaluation_id, assigned inside the
  transaction. The ordered population must match the items exactly; the report
  includes `items_digest` over the original item list. Duplicate opportunities or
  resolved game/horizon pairs fail. Maximum 1000 items, 10 MiB aggregate items;
  each canonical record/manifest is also limited to 10 MiB. This is not an evaluator.
- `references.reference(db, table, key)` fingerprints an explicitly selected row,
  including kind/parser fields where present and fetch UUID/sequence/raw hash/time.
  Composite settlement-observation keys preserve exact supporting observations.
  `references.validate_references` validates every nested typed reference, including
  raw bytes and transitive synthetic mode. It never selects a latest version or
  treats a body hash/sequence as an availability receipt.
- `journal.integrity(db)` validates schema, canonical digests, projected keys,
  nested references, parent modes/cohorts, typed settlement evidence, and complete
  report/item manifests. Use a caller-owned consistent read snapshot. It always
  reports `materialization: not_implemented`, `publication_verified: false`.

SQLite FKs, unique keys and UPDATE/DELETE/replacement rejection preserve history.
Exact retries return original IDs; same key with different content raises
`IdempotencyConflict`. Retry the original envelope, including original clocks/run
identity; a retry must not invent an earlier timestamp or replace a veto. Binding
chains return the current row for an unchanged semantic/evidence tuple; A→B→A
appends three revisions. Evaluations append corrections with the same frozen cohort.
Decision keys exclude schedule/input/ticker/time, so changing those cannot create a
second game/horizon forecast. Diagnostic corrections need a distinct dataset and
cannot be forward replacements. One immutable primary publication per decision.

Supplied clocks and modes remain caller assertions: these low-level APIs neither
sample post-commit visibility nor establish forward provenance. Basic contradictions
are rejected (for example a trusted receipt predating its subject or a timely claim
without the required publication/receipt). The later operational layer must supply
system times, clock checks, independent discovery completeness, sticky schedule
selection, current evidence ceilings and all transitive receipt/entry-digest bounds.
It must choose the correct latest settlement observation and establish the exact
contract/game join; F3.2 only checks supplied reference types and label consistency.
Matching stored references do not prove those later selection/coverage policies.

Production callers must hold the existing canonical `storage.writer_lock` around
journal/migration writes. Helpers additionally serialize through SQLite, cap busy
wait at 5s and own a 30s progress-handler deadline on a dedicated connection, checking
between evaluation items. No nested commits, external calls, retry loop or new lock
implementation. Portable tests prove SQLite serialization, not native flock.

## Compatibility and explicit migration paths

| Operation | Schema 1 | Schema 2 | Schema 3 | Other versions |
|---|---|---|---|---|
| Ordinary Store/collection/health/replay | Preserve | Preserve | Preserve; storage integrity checks available | Reject |
| Settlement migration | Explicit 1→2 | No-op | No-op, never downgrade | Reject |
| Forecast migration | Refuse; request settlement migration first | Explicit 2→3 | Validated no-op | Reject |
| Forecast journal | Refuse | Refuse | Supported storage envelopes | Reject |
| Ops inspection/backup/restore | Supported | Supported | All seven tables and reference checks | Reject |

Fresh `db-init` still creates schema 1. Nothing upgrades an existing archive on
initialization, collection, health, inspection or replay. The only new CLI is
`forecast migrate`; no tick/generation/evaluation/inspection CLI was added. It
preflights read-only under the existing writer lock, refusing absent/empty/schema-0
paths before Store can initialize them. Schema 1→2→3 requires **two explicit steps**.
Schema-3 collection still has the original behavior; it does not produce independent
discovery summaries or receipts until the separately scoped future increment.

Replay continues existing collector/settlement normalization and preserves research
rows; schema-3 results explicitly include
`forecast_storage.replay: preserved_not_reconstructed`. It does not regenerate
decisions, publication times, receipts or evaluations. Core health now positively
allowlists collect/discover/live-check, so research activity cannot mask failure.

For an independently created **disposable** archive only (never the deployed DB):

```sh
.venv/bin/vader --db /absolute/disposable/research.sqlite3 db-init
.venv/bin/vader --db /absolute/disposable/research.sqlite3 settlement migrate
.venv/bin/vader --db /absolute/disposable/research.sqlite3 forecast migrate
.venv/bin/vader --db /absolute/disposable/research.sqlite3 forecast migrate  # migrated=false
.venv/bin/python ops/manage.py inspect --database /absolute/disposable/research.sqlite3
.venv/bin/python ops/manage.py backup --source /absolute/disposable/research.sqlite3 --destination /absolute/disposable/backup-new.sqlite3
.venv/bin/python ops/manage.py restore --source /absolute/disposable/backup-new.sqlite3 --destination /absolute/disposable/restored-new.sqlite3
```

Backups use SQLite's supported backup mechanism to new destinations, retain all
legacy/research tables and reopen for validation. No archived observation is removed.
A schema-2 binary is not a code-only rollback for schema 3. Preserve the expanded
archive, use compatible code/fix forward, or inspect a separately restored backup;
never overwrite current evidence with an older copy or lower user_version/drop tables.

## Initial verification at 8640a29 (before the P2 fixes)

Windows, Python 3.12.14, private `.venv`, existing uv 0.12.19 with `uv sync --locked`.
No lockfile changes, dependency upgrade, fake POSIX lock or alarm shim.

```powershell
.venv/Scripts/python.exe -m pytest --noconftest -q tests/forecast_storage tests/forecast tests/evaluation
.venv/Scripts/python.exe -m unittest discover -s tests/ops -q
.venv/Scripts/ruff.exe check src tests ops
.venv/Scripts/ruff.exe format --check src tests ops
# Existing pinned uv executable:
../forecast-policies/.venv/Scripts/uv.exe build
git diff --check
```

**204 passed, 1 skipped module**: 52 new portable storage tests plus 152 F3.1 tests.
Operations: **31 tests run, 1 skipped** (30 passed). Lint/format/diff checks passed.
Wheel and source distributions built; wheel inspected for SQL 003 and all four new
storage modules. Fixtures populated every legacy table, repeated retrievals and
A→B→A settlement history. Tests compare every table after 2→3 and backup/restoration
for schemas 1/2/3; inject DDL and insertion failures; exercise real concurrent SQLite
connections; reject conflicting keys, malformed/incomplete records, missing/wrong
references, duplicate games, synthetic promotion and immutable-row replacement.

`--noconftest` avoids the existing Unix-only root conftest. A normal invocation of
the new native integration module failed during conftest import with
`ModuleNotFoundError: fcntl`; this is recorded as **blocked, not passed**. The legacy
SQL seed on Windows is not a claim that the existing Unix settlement CLI ran here.

## Outstanding Mac verification (required before merge/deployment)

Run on a separate disposable checkout/environment, never the deployed collector:

```sh
uv sync --locked  # use existing pinned uv 0.12.19
.venv/bin/python -m pytest -q tests/forecast_storage/test_store_initialization.py
.venv/bin/python -m pytest -q tests/forecast_storage/test_p2_regressions.py tests/forecast_storage/test_native_integration.py
.venv/bin/python -m pytest -q tests/forecast_storage tests/forecast tests/evaluation
.venv/bin/python -m pytest -q tests/test_archive.py tests/test_cli.py tests/test_integration.py tests/settlement
.venv/bin/python -m unittest discover -s tests/ops -q
.venv/bin/ruff check src tests ops
.venv/bin/ruff format --check src tests ops
uv build
# Use a new environment name if this one already exists; never overwrite it.
uv venv .venv-wheel-f32-init
uv pip install --python .venv-wheel-f32-init/bin/python dist/vader_intelligence-0.1.0-py3-none-any.whl
.venv/bin/python tests/integration/wheel_smoke.py --python "$PWD/.venv-wheel-f32-init/bin/python"
```

Native tests cover populated real Store/fixture 1→2→3, no downgrade, health exclusion,
writer-lock contention, restored replay retaining all research rows, explicit CLI
migration, and installed-wheel schema-3/unknown-version behavior. No native result
or production deployment is claimed by this handoff. Independent review remains.

Applied pinned Cool Coder `implement`, `code-review`, `data-systems-design`,
`systems-programming`, `security-engineering`, version 1.2.0 at
`e46e79805be0ee5877fa9bc993492064bbb40aa5`; shared installations unchanged. Main
review gates: H-01/03 serialization/SQL uniqueness, H-09 additive compatibility,
H-14 explicit retry conflicts, H-37/38 immutable history, V-59/62 bound typed JSON
and parameterized SQL. User-authorized F3.2 scope supersedes historical workstream
ownership/executor phases. Stop after this draft: no F3.3, scheduler or deployment.
