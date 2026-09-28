# Collection operations handoff

## Baseline and scope

- Baseline: `f0b0419ba45d1aeeb4ca663f6274981df09397d7`.
- Branch: `feat/collection-ops`, isolated clone on Windows, 2026-09-28.
- The supplied SHA is the checkpoint merge already on `main`; PR base is `main`.
  This supersedes the baseline handoff's historical unmerged-checkpoint description.
- Owned paths: `ops/`, `tests/ops/`, `docs/operations.md`, this handoff.
- Core collector, migrations, settlement and dependency lock remain unchanged.
- No production database, Mac checkout or running service is available here.

## Plan and decisions

1. Wrap the existing `python -m vader_intelligence.cli --config ... --db ... collect --once`.
2. Generate an absolute-path launchd agent and fail closed on conflicting services.
3. Bound the process deadline, output and history. Preserve the existing writer lock.
4. Interpret JSON plus exit status, with read-only eligibility queries for empty runs.
5. Use SQLite backup into new destinations and verify schema/integrity after reopening.
6. Test with disposable databases and subprocesses; review, commit, push, draft PR.

Default cadence: 120 seconds, deadline 90 seconds, termination grace 5 seconds.
The baseline recorded about 17 seconds for 30 retrievals/16 books over 8 games.
At that load, cadence averages 0.25 retrievals/second; the collector still enforces
its 2 requests/second cap. This is a measured sample, not a worst-case runtime.
Larger universes and retries can hit the deadline and must be reported as failures.

No wrapper retry loop or automatic resume: each scheduled pass starts a new run.
Interrupted evidence stays in the archive. A missed interval is never backfilled.
Absence of books is not an operational failure when complete discovery proves an
empty eligible universe. Gap attribution defaults to unknown.

## Engineering skills

Private skill checkout at version 1.2.0, commit
`e46e79805be0ee5877fa9bc993492064bbb40aa5`:
`systems-programming`, `reverse-branching`, `code-review`.
Applied the systems failure and reversal catalogs, plus the applicable concurrency,
clock, background-job, input and logging sections of `data-systems-design` and
`security-engineering` reference catalogs. Used the upstream manual folder-copy
installation procedure in a new private `work/.agents/skills` directory.
Shared skill installations are untouched. No skill/toolchain upgrade was made.

## Progress

- Read baseline, AGENTS.md, README, collector CLI, configuration, collector and schema.
- Verified the branch starts at the exact requested baseline.
- Windows has no WSL installation; native launchd validation must run later on Mac.
- Implemented separate CLI, bounded runner/history, launchd lifecycle and SQLite backup/restore.
- First portable suite: 22 tests passed in 10.068 seconds on Windows/Python 3.12.
- Tests use actual temporary WAL databases and subprocess locks. launchctl is mocked.
- Fixed Windows backup fsync by opening the completed destination with a writable descriptor.
- Added an opt-in native Mac launchd drill with synthetic input and a random test label.
- Added the complete runbook at `docs/operations.md`.
- Final portable suite: 28 tests passed in 13.063 seconds, Python 3.12.14 on Windows.
- Ruff 0.16.9: `ruff check src tests ops` passed; compileall and `git diff --check` passed.
- Added inventory, startup-exit failure, modified-config stop, preflight rejection,
  and end-to-end CLI backup/restore checks during review.
- Tiny WAL smoke measurements: backup 0.063 seconds; separate restore 0.015 seconds;
  schema 1, integrity OK, one blob restored with identical research bytes. Source retained.
- Synthetic empty collector pass: 0.250 seconds, exit 3, successful empty discovery.
- No new live-runtime measurement: approximately 17 seconds is prior baseline evidence.

Commands actually run from the isolated clone on Windows:

```powershell
& ../venv/Scripts/ruff.exe check src tests ops
& ../venv/Scripts/python.exe -m unittest discover -s tests/ops -v
& ../venv/Scripts/python.exe -m compileall -q ops tests/ops
git diff --check
```

The environment is private to this job. Core pytest tests were not rerun on Windows:
the baseline imports Unix-only `fcntl` and uses process alarms. No compatibility
shim was added to core code. Native launchd and POSIX signal/lock checks remain
explicit Mac validation steps. Windows uses actual Windows locks and termination;
launchctl calls are mocked. These are not native deployment evidence.

## Integration requests

No shared changes are required for baseline schema 1. Proposed integration work:

1. Before enabling settlement schema 2+, coordinate the collector's migration runner
   and `ops/archive.py` schema guard/column expectations. Both currently refuse newer
   databases. Add old/new-schema backup, restore, replay and collector tests together.
   Do not point this branch's collector at a separately migrated production archive.
2. Consider additive per-pass CLI fields `discovery_complete`, `eligible_contracts`
   and `eligible_games`. Define them at the eligibility check time, including cutoff
   crossings and resumed passes. Persist them in the run summary and preserve current
   exit codes. Operations could then replace its verified read-only eligibility query.
   Example empty result: status `inconclusive`, errors `[]`, books_this_pass `0`,
   discovery_complete `true`, eligible_contracts `0`, eligible_games `0`.
   Missing/inconsistent evidence must remain unverified, never healthy by default.
3. Consider shared core health fields separating activity/discovery from live-book
   freshness. The baseline's core `health` remains unchanged and can return unhealthy
   after a valid empty discovery. The operations wrapper handles that distinction.

The 120-second schedule, caps and supported schema must be revalidated against
the combined integration branch. No dependency, lockfile, migration or CLI edits
are hidden in this workstream.

## Deployment and reversal

Scheduler status: not installed or running by this session; Mac status unverified.
The operator must deploy reviewed code to the stable Mac checkout before installation.
Stop/uninstall the agent to reverse scheduling; retain all archive and backup files.
Code reversal does not undo collected observations. Production RPO/RTO are unmeasured.

## Code review: scoped operations changes

### Summary and scope

Reviewed all changes against the exact baseline and the requested owned paths.
The wrapper is independent of the private collector implementation except for
documented CLI output and explicit read-only schema-1 queries. No research archive
is used as a test target. All changed paths are owned; no excluded-path edits.

### Findings and fixes

No outstanding critical implementation findings. Fixed material issues discovered
during verification/review: backup flush descriptor mode on Windows, handling of
malformed result types, visibility of launchd failures before runner startup, and
ability to stop/uninstall after an accidental operations-config edit. Restart still
requires an unchanged config. These fixes have regression coverage.

No outstanding medium-risk code findings or nits. Native platform validation is
pending, so this is ready for draft review, not a verified production deployment.

### Hazard, failure and vulnerability scan

| Gate | Evidence and enforcement | Result |
|---|---|---|
| H-01/H-02/H-39 concurrency and overlapping jobs | `ops/common.py:48`, `ops/launchd.py:171`, `ops/runner.py:149`: nonblocking per-database locks and exclusive persistent registration; real competing-process test. | Protected for local managed jobs. |
| H-15/H-17/H-21 deadlines/retries/clocks | `ops/runner.py:69`: monotonic deadline; bounded termination; no wrapper retry multiplication. Gaps use wall time and declare unknown cause. | Tested on Windows; native signal drill pending. |
| H-32 ambiguous dual completion | `ops/runner.py:149`: archive is source of truth; sidecar start is not a claim of completion. Unknown completion retained after crash. | No fabricated success. |
| S-07/S-11/S-12 metadata durability | `ops/common.py:27`: same-directory temp, fsync, replace/link, directory sync on Unix. | Applied; no power-loss guarantee claimed from tests. |
| S-25/S-27/S-28 child lifetime and command injection | `ops/runner.py:69`: argv arrays, child wait, negative collector exit preserved, bounded output. | Tested subprocesses with spaces and ampersands. |
| V-59/V-62 input and resource limits | `ops/common.py:77`, `ops/archive.py:91`: numeric bounds, canonical paths, SQL value parameters, bounded capture/history. | No shell execution of config strings. |
| V-55/V-56/V-58 logging | `ops/runner.py:149`: local diagnostic evidence only; no secrets configured; bounded public-collector output. | Not a tamper-proof/off-host audit system. |
| R-23/R-29/R-48 config and recovery | `ops/archive.py:66`, `ops/launchd.py:60`: schema guard, new-path restore, integrity checks, config digest and preflight. | Real WAL restore exercised; production timing unmeasured. |
| R-35/R-38 automation reversal | `ops/launchd.py:236`: stop/disable/bootout before removal; preserved archive; no automated data repair/deletion. | Lifecycle simulated; native drill supplied. |

Lock order: lifecycle takes install lock, then operations run lock, then briefly
the existing collector writer lock while draining. Runner takes only its run lock;
the child takes the core writer lock. No path takes them in reverse order.

### Scope comparison

| Requirement | Evidence |
|---|---|
| Scheduled collection and exact commands | `ops/launchd.py`, `ops/manage.py`, `docs/operations.md` |
| Meaningful health, stale activity and gaps | `ops/archive.py:91`, `ops/runner.py:222` |
| Bounded logs, no archive deletion | Fixed log slots and retained history in `ops/runner.py` |
| SQLite backup and separate restored inspection | `ops/archive.py:66`; real WAL and CLI restoration tests |
| Paths, arguments, overlap and exit codes | 28 isolated operations tests |
| Native failure injection without production disruption | Opt-in `tests/ops/mac_launchd_smoke.py`, random test label and synthetic child |

### Missing validation and next steps

1. Review this draft against `main` at the requested baseline. Do not merge automatically.
2. Integrate reviewed code into the stable Mac checkout without switching another
   session's working directory. Resolve schema integration first if needed.
3. In that checkout run `.venv/bin/ruff check src tests ops`, `.venv/bin/pytest -q`,
   and `.venv/bin/python -m unittest discover -s tests/ops -v`.
4. Run `.venv/bin/python tests/ops/mac_launchd_smoke.py`. Check actual launchctl output
   compatibility, deadline termination and restart evidence before live installation.
5. Follow the exact configure/install/status commands in `docs/operations.md`, after
   verifying the actual production DB, environment and other potential schedules.
6. Back up that archive into a new path, restore separately, inspect and record the
   measured production-size timings. Select backup cadence/off-host protection.
7. Inspect `status` later for retained invocation history and gaps. No 24-hour coverage
   or production RPO/RTO is claimed here. Keep reports before the 1000-run rotation.

Verdict: ready for draft review; native deployment validation remains pending.
