# Settlement and collection-operations integration

Validated on this Mac on **2026-09-30**. Integration branch: `feat/integration-v1`.
The repository is intentionally **public**. Visibility was not changed.
This is an integration/review checkpoint, **not a production deployment**.

## Ancestry and scope

Both fetched heads exactly matched the requested inputs; there were no additional
worker commits to reconcile:

| Input | Verified commit | Base |
|---|---|---|
| Current main | `f0b0419ba45d1aeeb4ca663f6274981df09397d7` | Merge of checkpoint PR #1 |
| [Settlement PR #2](https://github.com/JustGooogleIt/Vader-Intelligence/pull/2) | `812a38a760dba328359db9669d9fcd09f451f388` | `checkpoint/milestone-1-reviewed` at `04c57eed37261b8ed94b27cc325699eb2ad456f7` |
| [Operations PR #3](https://github.com/JustGooogleIt/Vader-Intelligence/pull/3) | `4a46b166e0b984307a851694f17fc010cf0d5747` | Current main `f0b0419` |

Main already contains the checkpoint. Integration started at current main in
`/Users/ashwin/Documents/github-repos/Vader-Intelligence-integration-v1`, with its
own environment and databases. Merge commits `e9f426c` and `f397607` preserve both
histories without duplicating the checkpoint. Neither worker checkout/branch was
changed; main and the original PRs were neither merged into nor closed.

The user explicitly authorized shared integration fixes, superseding the earlier
parallel ownership restrictions. `executor.md` records the I1 interface/failure
contracts. No new dependency, lockfile revision or database migration was added.
No forecasting, execution, agent orchestration or prompt optimization is included.

Applied Cool Coder skills: implement, code-review, data-systems-design,
systems-programming and reverse-branching, **1.2.0** at
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. All 37 files across those installed
folders matched the pinned archive on read-only comparison. No shared skill
installation or unrelated tooling was changed.

## Compatibility fixes

`ops/archive.py` now explicitly accepts schemas **1 and 2**, validates their
migration records and expected columns, and includes every settlement table in
inspection/counts. Unknown versions remain rejected; core Store also rejects
invalid negative versions. Ordinary collection, health and inspection do not
migrate schema 1. `vader settlement migrate` is still the only explicit expansion.

SQLite online backup copies the whole database from a stable read snapshot; it
does not reconstruct selected tables. Reopened verification checks SQLite integrity,
foreign keys and every raw body's hash/length. Schema-2 tests compare **all rows**,
including revisions, observations, targets, request checkpoints, raw links and
ingestion sequence, before/after backup, separate restore and repeated replay.
The old schema-1 binary cannot read schema 2: a code-only rollback to it is invalid.

The collector now persists additive per-invocation summary fields:

| Field | Exact meaning |
|---|---|
| `discovery_complete` | Exhaustive bounded market pagination, successful schedule retrieval and an eligibility decision for every candidate in this invocation. Not a claim that every game matched or every book succeeded. |
| `eligible_contracts`, `eligible_games` | Distinct positive eligibility decisions in this invocation, at decision time. Later cutoff crossings do not erase them. |
| `eligibility_after_id` | Exclusive global eligibility-row boundary before this invocation. |
| `eligibility_through_id` | Inclusive boundary at completion; always filter by this run ID too. |
| `books_this_pass` (existing) | Successfully normalized books in this invocation, independent of earlier attempts on the same run. |

Operations requires matching persisted CLI output, kind `collect`, complete current
discovery and counts corroborated against that exact row range. Missing/inconsistent
fields fail closed. Historical eligibility on resumed runs cannot spoil or establish
current empty-universe evidence. Missing discovered contracts in an event response
now produce partial collection. Positive eligibility followed by cutoff without a
book remains `inconclusive_with_eligible_games`; it is not healthy empty discovery.
Settlement refreshes cannot mask collector health or satisfy collection classifiers.
No change was made to pregame guards, quote arithmetic or settlement payouts.

Process supervision now kills remaining members of the child's process group even
when the leader already exited. Signal races with an already-exited group are benign.
Waits and output joins are bounded. Configuration requires deadline plus **two**
cleanup grace periods to fit the cadence. launchd `ExitTimeOut` is `2*grace + 5`.
The existing launchd instance, operations lock and core writer lock remain in use.

## Verification actually executed

Python 3.11.6, macOS 26.6 arm64, isolated `.venv`; dependencies from the existing
lock using uv 0.12.19. Commands below ran from the integration checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'uv==0.12.19'
.venv/bin/uv sync --locked
.venv/bin/ruff check src tests ops
.venv/bin/ruff format --check src tests ops
.venv/bin/pytest -q
.venv/bin/pytest -q --ignore=tests/ops
.venv/bin/python -m unittest discover -s tests/ops -v
.venv/bin/uv build
git diff --check
```

| Check | Result |
|---|---|
| Full combined pytest | **191 passed**, plus 10 unittest subtests; actually executed together |
| Core/integration pytest with operations excluded | **160 passed** |
| Standalone operations unittest | **31 passed** in 3.720 seconds |
| Ruff lint/format | Pass; 34 Python files formatted |
| Build | Source distribution and wheel built |
| Installed wheel, outside checkout | CLI initialization, fixture replay, migration, both replays, settlement inspect, health, backup/restore and future-schema rejection pass |
| Schema 1 / 2 | Actual synthetic collection and replay, backup/restore, unchanged archived rows through migration, populated settlement revisions and repeated replay pass |
| Invalid/future schemas | Core and operations reject -1, 3 and 999; no destination created or migration performed |

These are fresh integrated results, not summed claims from the previous handoffs.
Offline tests forbid HTTP; failure injection is synthetic. Tests cover resumed
empty runs, partial discovery, cutoff crossings, locks, dead-parent descendants,
raw corruption, missing schema tables, corrections and full table equality.

The wheel was installed into a separate environment with locked runtime dependencies:

```sh
.venv/bin/uv export --locked --no-dev --no-emit-project --format requirements-txt \
  --output-file data/integration/wheel-requirements.txt
.venv/bin/uv venv --python .venv/bin/python data/integration/wheel-env
.venv/bin/uv pip install --python data/integration/wheel-env/bin/python \
  --require-hashes -r data/integration/wheel-requirements.txt
.venv/bin/uv pip install --python data/integration/wheel-env/bin/python \
  --no-deps dist/vader_intelligence-0.1.0-py3-none-any.whl
.venv/bin/python tests/integration/wheel_smoke.py \
  --python "$PWD/data/integration/wheel-env/bin/python"
```

The smoke script verifies the import is from the installed environment, runs from
a temporary working directory with PYTHONPATH/PYTHONHOME removed, and never opens
a production database. Repeated package validation used `--reinstall` after the
final schema guard change.

### Native launchd evidence

```sh
.venv/bin/python tests/ops/mac_launchd_smoke.py
```

The supplied drill was run, then strengthened and rerun. Final native label:
`com.vader.test.collection.66252509f16e0969134d`, in a disposable directory. At
**08:26:23 UTC** the stubborn synthetic child ignored SIGINT. The wrapper ended
after **2.714 seconds**, child exit **-9**, wrapper/launchctl exit **124**. A second
wrapper invocation was rejected with **75**; duplicate bootstrap was rejected by
actual launchctl with **5**. Bootout, lock drain and child disappearance were verified.
At **08:26:26 UTC** a new bootstrap completed an empty synthetic pass in **0.153
seconds**, with wrapper/launchctl exit **3**. Only one invocation per bootstrap was
recorded. Both jobs were booted out; their disposable plists/databases were removed.

This exercises native launchctl behavior, not mocks. The production install
preflight was not bypassed for production: it correctly refuses a worktree. The
test explicitly bootstraps only a random test label. Production installer checks
remain enforced. Read-only inventory of saved user/system definitions, loaded
GUI/user/system jobs and this user's cron found **no collector jobs** after cleanup.
No production job was stopped, installed, started, migrated or switched. Another
host/user's opaque schedule cannot be ruled out by local inventory alone.

### Separate live checks and cadence decision

Real public endpoints were exercised serially, separate from synthetic tests. A
new schema-1 database was configured for a **manual** bounded operations invocation;
configuration does not install a job:

```sh
.venv/bin/vader --db data/integration/live.sqlite3 db-init
.venv/bin/python ops/manage.py configure \
  --checkout "$PWD" --python "$PWD/.venv/bin/python" \
  --database "$PWD/data/integration/live.sqlite3" \
  --collector-config "$PWD/ops/collector.example.toml" \
  --state "$PWD/data/integration/live-ops-state" \
  --output "$PWD/data/integration/live-ops.json" --test-label
.venv/bin/python ops/manage.py --config "$PWD/data/integration/live-ops.json" run
.venv/bin/vader --db data/integration/live.sqlite3 settlement migrate
.venv/bin/vader --config ops/collector.example.toml --db data/integration/live.sqlite3 \
  settlement refresh --ticker KXMLBGAME-26SEP271505LADSF-LAD \
  --ticker KXMLBGAME-26SEP271520BALNYY-NYY --budget 60
.venv/bin/python ops/manage.py --config "$PWD/data/integration/live-ops.json" run
```

| Live run, 2026-09-30 UTC | Actual result |
|---|---|
| `a68c41d4-19e2-4452-bf60-1fce9d381189`, 08:22:50–08:22:59 | Schema 1: **8 books / 4 games**, 18 retrievals, complete discovery, no errors; wrapper **9.579 s** |
| `5fd00314-3899-4b94-8cde-bc817fb1d3b3`, 08:24:00–08:24:04 | Schema 2 settlement: two targets, one mapped and one quarantined, no retrieval errors. LAD YES payout `1.0000`; NYY scalar payout `0.5300`, no binary label. |
| `d410cacf-a2f6-48d8-ba47-76649fa8adaf`, 08:26:04–08:26:12 | Schema 2: **8 books / 4 games**, 18 retrievals, complete discovery, no errors; wrapper **9.087 s** |

The later negative-schema guard and launchd shutdown-timeout edits do not alter the
supported-schema collection/parser behavior sampled above. Final offline tests and
installed-wheel checks include those changes. Source hashes remain in each run's
immutable provenance. No live correction arrival or network outage was induced.

**Decision:** retain 120-second cadence / 90-second child deadline / 5-second grace
as a best-effort proposal for this measured workload. Cleanup is budgeted to 100
seconds, leaving 20 seconds before the next nominal interval. Two samples are not
p95/p99 or endurance evidence. At 50 markets, a pass may need roughly 80–110 logical
requests; an 8-second request budget makes a full worst-case pass far longer than
90 seconds. The wrapper must terminate and report deadline/partial status, never
claim completeness or repeatedly resume automatically. Caps, retries and schedule
refreshes remain bounded. Do not promise complete snapshots every 120 seconds.

The two manual collection starts were over 120 seconds apart. The wrapper correctly
recorded the gap with unknown cause; no continuous schedule or fabricated backfill
is claimed. Empty live eligibility would have been inconclusive for book validation;
these particular live samples did contain eligible games.

### Live archive copy verification

```sh
.venv/bin/python ops/manage.py backup --source "$PWD/data/integration/live.sqlite3" \
  --destination "$PWD/data/integration/live-backup.sqlite3"
.venv/bin/python ops/manage.py restore --source "$PWD/data/integration/live-backup.sqlite3" \
  --destination "$PWD/data/integration/live-restored.sqlite3"
.venv/bin/vader --db data/integration/live-restored.sqlite3 settlement inspect
.venv/bin/vader --db data/integration/live-restored.sqlite3 replay
.venv/bin/vader --db data/integration/live-restored.sqlite3 replay
```

All source/restored table fingerprints matched, including raw BLOB bytes and
`sqlite_sequence`; both replays processed 44 responses with no errors or changed
fingerprints. Integrity, foreign keys and all raw hashes passed after reopening.
The archive contains 44 retrievals, 36 unique bodies, 69 settlement versions, 134
settlement observations, two targets and 16 collector eligibility rows. Copy plus
verification took **0.0091 s**, separate restore **0.0045 s** on this tiny archive;
these are not production RTO measurements. Local databases/logs/evidence JSON are
ignored artifacts under `data/integration/`, not committed payloads.

## Deployment order — prepared, not executed

### Scoped review

Reviewed both exact input histories and the integration changes, including schema
guards, backup/restore, collector/resume decisions, process control, launchd paths
and native test cleanup. No unrelated workstream files or data were changed.
No remaining critical or medium-risk code finding was identified; no polish-only
refactor was introduced. The production/endurance limits below are explicit missing
deployment evidence, not fabricated passing checks.

| Gate | Mechanism and evidence |
|---|---|
| H-09 schema compatibility / R-26 rollback | Explicit 1/2 guards in `ops/archive.py:63`; additive migration only; unknown/negative-version tests and old-binary boundary |
| H-14 repeat/replay effects | Unique retrieval observations preserved; `tests/test_integration.py:33` compares all rows across both schemas and replay |
| H-20 ordering / H-39 overlap | Sequence-bounded current-pass decisions; existing stable lock inodes; actual native overlap and duplicate bootstrap rejection |
| H-32 multi-record consistency / S-07 durability | SQLite transactions/FULL retained; online backup snapshot and reopened raw/FK verification |
| H-15 bounded execution / S-25 child lifetime | `ops/runner.py:37` and `:76`; group termination and shared cleanup deadlines, native SIGKILL proof |
| Untrusted inputs and paths | Existing fixed HTTPS GET allowlist, SQL values bound, subprocess argv arrays, absolute paths; no new credentials or provider endpoints |

Findings fixed during integration: schema-1-only operations guard, historical
eligibility contaminating resumed health, omitted candidates falsely appearing as
empty discovery, and descendant processes surviving an exited leader. Each fix has
regression coverage. Review readiness: **ready for independent review**, without
permission to deploy, merge either original PR, or claim production RPO/RTO.

### Operator procedure

One operator owns this maintenance window. Independently review the draft and the
two original PRs first. Identify the actual canonical production archive and current
service/configuration; do not assume a historical `data/live.sqlite3` is production.
Coordinate other users/hosts before public provider work. Replace the capitalized
values below with reviewed absolute paths; every backup/restore/config/state path
must be new. Record the chosen final integration SHA from the PR.

1. Prepare a **stable standalone checkout** at that SHA, outside temporary/worktree
   directories, with its own locked environment. Preserve the previous executable,
   config, plist definition and archive. Preparation must not install a job.
2. If an existing managed collector exists, use its current operations config to
   `stop`; verify bootout and both lock drains. Stop any separately authorized writer
   through its own owner. Never kill an unrelated process to bypass a lock.
3. While writers remain stopped, use the **new compatible** operations tool to take
   a uniquely named backup, restore it to a separate path and inspect it. Require
   successful schema/integrity/raw-hash/FK checks and matching table counts. On the
   copy, run replay and settlement inspection (if schema 2); verify no row-count
   change. Record timing and backup timestamp. Investigate any failure before moving on.
4. Retire the old managed registration with its `uninstall` command, retaining its
   saved configuration/definition and all data. Select the new compatible executable.
   If settlement is desired and the archive is schema 1, **explicitly migrate** only
   after the verified backup. If it is schema 2, preserve it; never force a downgrade.
5. Configure a new dated operations config/state referencing the stable checkout,
   its absolute `.venv/bin/python` (do not resolve its symlink), the canonical archive,
   and reviewed collector TOML. Review `plist`, then install **one** collector job.
6. Inspect status after one bounded invocation. Require complete current evidence or
   verified empty discovery, not merely a loaded job. Observe runtime/partials/gaps
   before any cadence change. Later run a separately authorized 24-hour endurance
   and production-size backup/restore drill.

Commands for the chosen paths, in the order above:

```sh
# Only when a previous managed service exists:
"$VADER_OLD_PY" "$VADER_OLD_OPS" --config "$VADER_OLD_CONFIG" stop

"$VADER_NEW_PY" "$VADER_NEW_OPS" inspect --database "$VADER_DB"
"$VADER_NEW_PY" "$VADER_NEW_OPS" backup --source "$VADER_DB" --destination "$VADER_BACKUP"
"$VADER_NEW_PY" "$VADER_NEW_OPS" restore --source "$VADER_BACKUP" --destination "$VADER_VERIFY_DB"
"$VADER_NEW_PY" "$VADER_NEW_OPS" inspect --database "$VADER_VERIFY_DB"
"$VADER_NEW_PY" -m vader_intelligence.cli --db "$VADER_VERIFY_DB" replay
# Schema 2 copy only:
"$VADER_NEW_PY" -m vader_intelligence.cli --db "$VADER_VERIFY_DB" settlement inspect

# Only when replacing a previous managed service:
"$VADER_OLD_PY" "$VADER_OLD_OPS" --config "$VADER_OLD_CONFIG" uninstall
# Explicit and only if the operator elects settlement/schema 2:
"$VADER_NEW_PY" -m vader_intelligence.cli --db "$VADER_DB" settlement migrate

"$VADER_NEW_PY" "$VADER_NEW_OPS" configure --checkout "$VADER_NEW_ROOT" \
  --python "$VADER_NEW_PY" --database "$VADER_DB" \
  --collector-config "$VADER_NEW_ROOT/ops/collector.example.toml" \
  --state "$VADER_NEW_STATE" --output "$VADER_NEW_CONFIG" \
  --cadence 120 --deadline 90 --grace 5
"$VADER_NEW_PY" "$VADER_NEW_OPS" --config "$VADER_NEW_CONFIG" plist
"$VADER_NEW_PY" "$VADER_NEW_OPS" --config "$VADER_NEW_CONFIG" install
"$VADER_NEW_PY" "$VADER_NEW_OPS" --config "$VADER_NEW_CONFIG" status
```

Installation preflight checks clean baseline ancestry, stable location, interpreter
import path, config, database and competing schedules. Do not bypass a rejection.
`ProgramArguments` and working directory are absolute; the child gets checkout/src
as PYTHONPATH, unbuffered output and no PYTHONHOME. launchd does not need an interactive
shell, activation command, shell expansion or PATH lookup for the interpreter.

## Settlement coordination and rollback boundaries

For later manual settlement, retain the registration and stop/drain the same job,
then run one bounded refresh against the same canonical archive. Inspect the result
and any failure; when the process has exited and locks drained, restart the same
job. No second schedule, maintenance retry loop or simultaneous other-database
provider workload is introduced:

```sh
"$VADER_NEW_PY" "$VADER_NEW_OPS" --config "$VADER_NEW_CONFIG" stop
"$VADER_NEW_PY" -m vader_intelligence.cli --config "$VADER_NEW_ROOT/ops/collector.example.toml" \
  --db "$VADER_DB" settlement refresh --ticker "$VADER_REVIEWED_TICKER" --budget 60
# Inspect errors/quarantines and confirm process exit before restarting:
"$VADER_NEW_PY" -m vader_intelligence.cli --db "$VADER_DB" settlement inspect --limit 20
"$VADER_NEW_PY" "$VADER_NEW_OPS" --config "$VADER_NEW_CONFIG" restart
```

The operator can reverse scheduling by stop/uninstall without deleting data.
For schema 2, roll forward with repaired compatible code or return to a previously
reviewed **schema-2-compatible** executable. The old schema-1 binary is not a rollback.
Do not drop settlement tables or reset `user_version` to make it accept the archive.

A backup restores only observations committed at its snapshot. An older backup
**does not preserve newer/post-migration observations**. Preserve the current archive
and its WAL state using a safe SQLite backup after draining writers, retain both
snapshots, restore separately, and explicitly assess the missing interval before
selecting any restored database. Do not overwrite the current archive or claim lossless
rollback. No production data restore or RPO/RTO was measured in this task.

Remaining limits: 24-hour endurance, actual sleep/network outages and production
restore scale remain unverified; those failures were only injected synthetically.
Conflicting settlement schedule variants stay quarantined. No automatic cross-window
mapping or manual override is added. Deadline enforcement bounds children, not arbitrary
kernel/disk stalls during later metadata persistence or full archive integrity scans.
Ready for independent code review; deployment awaits that review and a separate action.

## Publication

Published `feat/integration-v1` with implementation checkpoint
`214db1488ef7db7853577e003df97220604e7d6c` and opened
[draft PR #4](https://github.com/JustGooogleIt/Vader-Intelligence/pull/4) against `main`.
The PR links the original settlement PR #2 and operations PR #3. Both original
workstreams remain open and their branch heads unchanged. Repository visibility
remains public, as requested. No production deployment was performed.

## PR #4 independent-review correction — 2026-09-30

This section is new verification after reviewed head
`9d3d1ea2f167d5154dd6c67b3f47f2c7640fced7`; the earlier results above are historical
and do not claim to have tested this correction. Forecast specification work was
already checkpointed separately at `3c49998ac8356ec9c4d974292e2c4f8e03b92d82` on
`docs/forecast-evaluation-spec`; none of it is included in this fix.

### Reproduction and root cause

The independent reviewer reported a P2 inspection-provenance defect. On this native
macOS 26.6 arm64 host (Python 3.11.6), reconstructed the scenario from the existing
synthetic settlement fixtures: A creates a target and is interrupted before market
retrieval; B completes YES/$1; A resumes and retrieves corrected NO/$0. The referenced
`pr4-resume-repro.py` was not found in the searched repository/attachment directories
and was **not used**.

With only the two parametrized regression tests added to the reviewed code, both
failed. Correction case: `{'latest_result': 'no', 'evidence_result': 'yes'}`.
Unchanged case: both labels were YES, but `latest_evidence.market_fetch_id` still
pointed to B rather than A's later retrieval. This proves the problem is not merely
a result-string mismatch. Provider timestamps were unchanged in the fixture.

`settlement/service.py:inspect` selected the current semantic version correctly,
but chose its evidence through `settlement_targets ORDER BY rowid DESC`. Target
creation order is not retrieval order when earlier runs resume. Raw responses,
versions and observation associations were intact; no data repair is needed.

### Corrected evidence contract (inspection `evidence_version=2`)

- Each displayed version now has `first_evidence`, selected by that version's
  `first_fetch_id` **and its matching observation**, and `latest_evidence`, selected
  among observations of that exact kind/entity/version by descending `fetches.seq`.
  Parser version breaks ties within one retrieval. Provider timestamps are retained
  as provenance, not used as an ordering substitute.
- The latest observation of an old revision in `--history` stays within that revision.
  An A→B→A semantic history does not collapse equal payouts across distinct revisions.
  Repeated unchanged observations advance latest evidence while first evidence stays
  fixed. Existing top-level `first_fetch_id`, `seq` and `retrieved_at` still describe
  the version's first retrieval; latest retrieval time is inside `latest_evidence.observation`.
- Refresh context is obtained from that observation's request key and run ID, with
  exact target association checks: market fetch for Kalshi, mapping version plus
  anchor fetch for mapping, schedule fetch for MLB. There is no fallback to another
  target. Legacy target fields remain in each evidence object when associated.
- `status=available` means the supporting observation exists; `target_status` separately
  distinguishes `available`, `incomplete` and `missing`. Missing observation/target or
  mismatching association has an explicit reason. A directly supported market fetch
  can remain available even when refresh context is missing. Missing event/schedule
  fields remain null, never filled from an unrelated refresh.
- `latest_mapping` and `latest_mlb_result` are **independent latest context**, not
  assertions about what the displayed Kalshi revision knew. Each now carries its own
  first/latest evidence and run/retrieval identities. They may refer to another refresh
  (or be missing). The top-level result evidence's `mapping_version_id` belongs to its
  own target and is not an alias for the independently displayed latest mapping.
- Inspection uses one read snapshot, including direct library use; the existing
  read-only CLI snapshot remains supported. No archive writes, schema/migration,
  normalization, mapping algorithm or provider interpretation changed.

### Actual correction verification

Commands ran from the isolated integration worktree using its existing environment:

```sh
# Before the fix, with just the new interleaving regression: 2 failed.
.venv/bin/pytest -q -s tests/settlement/test_settlement.py -k inspection_resumed_target
# After the fix, all focused inspection cases:
.venv/bin/pytest -q -s tests/settlement/test_settlement.py -k inspection
.venv/bin/ruff check src tests ops
.venv/bin/ruff format --check src tests ops
.venv/bin/pytest -q
.venv/bin/uv build
.venv/bin/uv pip install --python data/integration/wheel-env/bin/python \
  --no-deps --reinstall dist/vader_intelligence-0.1.0-py3-none-any.whl
.venv/bin/python tests/integration/wheel_smoke.py \
  --python /Users/ashwin/Documents/github-repos/Vader-Intelligence-integration-v1/data/integration/wheel-env/bin/python
.venv/bin/python tests/ops/mac_lifecycle_smoke.py
git diff --check
```

The unchanged two-case regression now passes with NO/NO and YES/YES respectively,
and asserts the **actual latest fetch identity**, not just matching strings. Six
focused inspection tests pass: corrected/unchanged interleavings, A→B→A and repeated
observations, repeated replay with unchanged inspection/counts, independent older
mapping/MLB context during a newer interrupted refresh, missing associations, and a
populated read-only CLI inspection with absent mapping. The last also checks mismatched
target links do not supply unrelated context.

Final combined native pytest: **197 passed, 10 subtests passed in 12.79 seconds**.
This is an actual combined run, including the standalone operations test cases;
counts were not assembled from older runs. Ruff lint and format pass (35 Python
files). Source distribution and wheel build pass; freshly reinstalled wheel passes
the isolated outside-checkout CLI smoke. No live provider reads were required for this
inspection-only defect; all new settlement inputs are synthetic. Earlier live evidence
above remains unchanged and is not presented as a new live validation.

Ignored local evidence: `data/integration/pr4-provenance-before.txt`,
`pr4-provenance-after.txt`, `pr4-combined-tests.txt`, `pr4-wheel-smoke.json`,
`pr4-native-lifecycle.json` and `pr4-native-lifecycle.err`. No databases, logs,
generated artifacts or synthetic checkout commits are published.

### Full native lifecycle drill

The earlier native smoke verified direct bootstrap/deadline/bootout after completion;
it did **not** establish the full install/stop/restart path or active-child bootout.
New opt-in `tests/ops/mac_lifecycle_smoke.py` fills that specific gap without altering
production helpers or weakening preflight. It creates a standalone, clean Git checkout
outside the OS temporary directory, with a private interpreter and a committed,
explicitly synthetic CLI. Operations files are copied exactly from the implementation
under test. No public collector is executed. Real preflight checks ancestry, clean
tracked files, interpreter/import location, configuration, archive schema/integrity and
real service inventory. A production/ambiguous conflict would stop installation.

Actual run: **09:06:25.483997–09:06:38.557846 UTC**, 2026-09-30.
Unique label: `com.vader.test.collection.6c35c1f02589ef7b3f07`.
Disposable checkout's synthetic commit: `fd270909110922361efb2468e690d1722ff4abaa`
(local test artifact only, removed with the checkout).

- Real `configure` and `install` helpers passed with native launchctl and intact
  preflight. Duplicate install rejected (exit 1); concurrent `run` rejected (exit 75).
- Invoked real `stop` while the synthetic collector and descendant were active,
  ignoring termination signals and retaining the collector writer lock. The helper
  disabled/booted out the job; the wrapper killed the child group. Child exit −9,
  wrapper interrupted exit 130. Stop plus group/lock checks took **1.171 seconds**,
  well before the configured 60-second wrapper deadline.
- Both PIDs and process group were absent afterward. A separate process acquired
  both the operations-run and collector-writer locks, proving drain rather than
  merely assuming it from launchctl output.
- Real `restart` reran preflight/inventory and produced exactly one new invocation:
  synthetic empty discovery, collector/wrapper exit 3, **0.313 seconds**. History and
  independently recorded child invocations both showed exactly two total starts
  (one stopped stubborn job and one restarted empty job), without duplicates.
- Real `stop`/`uninstall` then removed this job's plist and registration. The archive
  remained until deliberate disposal of the test-owned directory. The disposable
  checkout, environment and data were removed; subsequent read-only inventory found
  no collector job. Only resources created by this drill were altered.

This tests genuine Mac lifecycle behavior with injected synthetic work, not a production
deployment or a 24-hour run. On cleanup failure the drill retains its own directory
and reports it for diagnosis rather than deleting files beneath a possibly active job.

### Scoped review and remaining limits

Applied `implement`, `code-review`, `data-systems-design`, `systems-programming`,
Cool Coder **1.2.0**, pin `e46e79805be0ee5877fa9bc993492064bbb40aa5`.
All 26 files in these installed folders matched the pin on read-only comparison;
no shared skill or unrelated tooling changed. The requested correction is the
authorization for this follow-up to I1; no forecast implementation is included.

Reviewed the complete correction diff: bounded observation lookups, exact association
checks, read-snapshot consistency (H-04/H-20/H-37), unchanged idempotent replay (H-14),
process-group/lock drain and real helper paths (H-39/H-48), scoped cleanup and checked
subprocess exits. No outstanding material issue found in this scoped review; the
secondary reviewer must independently review the newly pushed head before merge.

No schema change or production migration/service alteration. Existing source-hash
resume checks still require identical code: upgrading does not silently authorize
resuming a pre-upgrade unfinished run; retain its evidence and start a new refresh
under the new code when appropriate. Read-only inspection works on existing schema-2
archives. Production-size restoration, 24-hour endurance, real sleep/network outages
and unresolved provider mapping cases remain limitations from the earlier integration.
PR #4 stays draft; no merge or deployment is performed.
