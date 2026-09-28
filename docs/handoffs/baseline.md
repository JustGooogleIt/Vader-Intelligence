# Shared implementation baseline

Checkpoint date: 2026-09-28. Private repository:
https://github.com/JustGooogleIt/Vader-Intelligence.
Checkpoint branch: `checkpoint/milestone-1-reviewed`.

The final pushed SHA supplied in the checkpoint completion message is the common
baseline for BOTH jobs. Use that exact SHA, not a later moving branch tip. This
document is included in that commit; it cannot embed its own Git commit hash.
`main` contains only the project title and `.gitignore` because the remote was empty.
The implementation remains on the draft-PR branch until separately integrated.

## Current functionality

One Python process collects public Kalshi `KXMLBGAME` series, rule PDFs, markets,
events, official MLB schedule evidence and complete `depth=0` books. Only reviewed,
unambiguous, full-game $1 team-win contracts pass eligibility. Exact decimal strings
are preserved; opposite bid complements derive asks from one book response.

SQLite archives bytes/SHA-256, distinct retrieval IDs, request attempts/checkpoints,
versioned metadata, observations, eligibility and collector provenance. Replay is
idempotent. Writers use an adjacent process lock, WAL/FULL transactions and foreign
keys. Interrupted pagination is resumable; current eligibility is refreshed on
resume. Requests have allowlisted HTTPS GET routes, rate/size/page/attempt/deadline
bounds, Retry-After handling and terminal 401/403 failures.

Before **each** book attempt, including retries, the collector rechecks schedule
freshness and the two-minute cutoff. Late responses remain raw and are rejected
as pregame observations. Fixture imports cannot relabel retrieval provenance or
turn failed records into a successful re-import. Fixture runs do not mask live
health failures; archive-integrity failure is a failed live check even with no books.

Current database schema: **1**, migration `001_initial.sql`, `PRAGMA user_version=1`.
Parser version: **1**. Mapping version: **1**. Run-provenance interface version: **1**.
Future database versions are refused by the current collector. There is no migration
runner for version 2 yet; settlement must propose that integration explicitly.

Scheduler status: **none in the repository or installed for Vader on this Mac**.
Checkpoint inspection found no Vader plist in user/system LaunchAgents or
LaunchDaemons, and no loaded Vader job in `launchctl list`. `collect --once` exits
after one pass. Health becomes stale after the configured freshness window.

## Isolation and exact setup

Substitute the final 40-character pushed SHA before running these commands. Run the
worktree commands once from the existing checkout; if a path/branch already exists,
inspect it instead of overwriting it. Do not share the original ignored live database
between sessions. Do not copy a live SQLite file without accounting for WAL.

```sh
cd /Users/ashwin/Documents/github-repos/Vader-Intelligence
git fetch origin
VADER_BASELINE_SHA=REPLACE_WITH_FINAL_PUSHED_SHA
git cat-file -e "$VADER_BASELINE_SHA^{commit}"
git worktree add ../Vader-Intelligence-settlement -b work/settlement "$VADER_BASELINE_SHA"
git worktree add ../Vader-Intelligence-operations -b work/operations "$VADER_BASELINE_SHA"
```

Each job runs this setup independently in its own worktree:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'uv==0.12.19'
.venv/bin/uv sync --locked
.venv/bin/vader --db data/dev.sqlite3 db-init
```

Python 3.11+ on macOS/Unix is required. Verification used Python 3.11.6. Runtime
and development dependencies are pinned in `uv.lock`; no dependency/tooling upgrade
is part of the handoff. The build backend is declared in `pyproject.toml`; builds
are not claimed to be bit-for-bit reproducible across Python/toolchain versions.

## Verification commands and known results

```sh
.venv/bin/ruff check src tests
.venv/bin/pytest -q
.venv/bin/uv build
VADER_SMOKE_DIR=$(mktemp -d)
.venv/bin/vader --db "$VADER_SMOKE_DIR/fresh.sqlite3" db-init
.venv/bin/vader --db "$VADER_SMOKE_DIR/fresh.sqlite3" replay --fixture tests/fixtures/mlb.json
.venv/bin/vader --db "$VADER_SMOKE_DIR/fresh.sqlite3" replay
.venv/bin/vader --db "$VADER_SMOKE_DIR/fresh.sqlite3" health
```

The final health command intentionally returns **1/unhealthy**: synthetic books
are not fresh live evidence. The other smoke commands return 0. Leave fixture and
live databases separate. Global CLI options precede the subcommand.

```sh
.venv/bin/vader --db data/live-check.sqlite3 live-check
.venv/bin/vader --db data/live-check.sqlite3 health
.venv/bin/vader --db data/live-check.sqlite3 replay
.venv/bin/vader --db data/dev.sqlite3 collect --once
.venv/bin/vader --db data/dev.sqlite3 collect --once --resume RUN_ID
```

Exit codes: **0 complete; 1 failed/partial/unhealthy; 3 inconclusive; 130 interrupted**.
Resume requires the same command, configuration and source hash. New code/config
requires a new run. A live check is bounded by configured request/page/market caps;
an operations wrapper should also impose an overall process deadline.

Checkpoint checks actually run: 106 offline tests passed; Ruff passed; wheel and
source distribution built; CLI initialized fresh databases and imported/replayed
fixtures; the packaged wheel's CLI found its SQL migration. A disposable backup
of historical live evidence passed integrity and replay (86 responses, no changed
row counts). These are separate from the fresh live run below.

Fresh live run at **2026-09-28 09:00 UTC** succeeded: **16 books across 8 games**,
30 retrievals, no errors, hashes verified after reopening. All 16 books had valid
pregame timing. It used an outer 90-second process cap, request budget 8 seconds,
two attempts, 3 pages and 50 markets; completed in about 17 seconds. Exact config
path, run ID, timestamps and source hash: [live evidence](../checkpoint-live-evidence.json).
An empty eligible universe on a later date must return inconclusive; fixtures must
never substitute for that check.

## Source files and stable interfaces

| File / interface | Responsibility and constraints |
|---|---|
| `src/vader_intelligence/cli.py` | Public commands and JSON/exit-code contract: db-init, discover, collect --once, replay, health, live-check |
| `config.py`, `config.example.toml` | `[collector]` TOML; relative database path is relative to TOML file; no arbitrary hosts or credentials |
| `collector.py` | Run/session orchestration, current schedule and event checks, pagination resume, per-attempt eligibility guard |
| `scope.py` | Reviewed MLB aliases; conservative rule/date/time/game-number matching; `Eligibility` reasons and cutoff |
| `normalize.py` | Exact decimals, full book semantics, source entities, book-proof validation and parser version |
| `transport.py` | `Reader.get`, public GET allowlist, attempts, rate bounds, monotonic deadline and Unix alarm; optional pre-attempt guard |
| `storage.py`, `migrations/001_initial.sql` | `Store`, `writer_lock`, immutable source archive, transactions, recovery and schema guard |
| `replay.py` | Offline normalization and synthetic fixture import; no HTTP |
| `provenance.py` | `RunProvenanceV1`, stable JSON and package source hash |
| `tests/fixtures/mlb.json` | Explicitly synthetic fixture; never live evidence |

Core tables: `runs`, `requests`, `fetches`, `blobs`, `entities`, `observations`,
`eligibility`, `schema_migrations`. Source bytes are in `blobs`; every `fetches.id`
is a distinct retrieval, ordered by `fetches.seq`. Observations are unique by
retrieval/kind/ticker/parser version. Book JSON links `eligibility_id`, then the
official schedule and market retrievals. Runtime/completion details are in run
summaries; model/forecast/evaluation fields remain empty in collector provenance.

## Workstream A — settlement and event mapping

Goal: versioned, reviewable event mappings and separate official MLB results from
Kalshi finalization, settlement timing and payout semantics. Test ordinary games,
doubleheaders, postponements, cancellations, ambiguity and fractional payouts.
Archive underlying evidence; never infer a boolean outcome from a nonbinary payout.

Exclusive ownership during parallel work:

- New `src/vader_intelligence/settlement/` and `src/vader_intelligence/mapping/` modules.
- New additive migration files numbered `002_...` onward; preserve `001_initial.sql`.
- New `tests/settlement/`, `tests/mapping/` and correspondingly scoped fixtures.
- `docs/settlement/`, including `docs/settlement/integration-notes.md`.

Use an independent test database. New migrations remain opt-in in isolated tests
until integration: the baseline collector rejects schema >1. Document required
migration-runner, parser-version, allowlist, shared CLI or `scope.py` changes in
integration notes with proposed signatures, compatibility and migration tests.
Do not silently change core interfaces or apply migrations to the operator's archive.
Keep scoring/forecasts and trade execution out of this workstream.

## Workstream B — collection operations, scheduling, health and backups

Goal: wrap the existing CLI for restartable Mac collection, nonoverlap, meaningful
health/gap reporting, bounded logs/storage monitoring, and tested SQLite backups
and restoration. Demonstrate interruption/restart and eventually 24-hour operation.

Exclusive ownership during parallel work:

- New `ops/` scripts, launchd templates, status/log tooling and configuration examples.
- New `tests/operations/` and `docs/operations/`.
- `docs/operations/integration-notes.md` for shared-interface proposals.

Invoke the collector via its CLI and interpret JSON plus exit status. **Do not edit
core storage, shared CLI, collector/parser/mapping code, existing tests, migration
files or schema during parallel work.** Use read-only inspection where needed and
SQLite's backup API/command, not raw copying of an active database file. Keep any
wrapper state in a separate operations file/database. Isolate fixture and live
health. Respect the current per-database writer lock and explicitly control a
single production schedule; independent databases do not share a rate limiter.

Do not enable two production collectors to prove parallelism. Build/test schedules
in an isolated environment first. Record requested shared health fields or CLI
options in integration notes with compatible examples and failure behavior.

## Integration protocol and unresolved limitations

Both jobs begin at the same final SHA and create independent feature branches.
Commit only owned paths. Avoid simultaneous edits to root `README.md`, `plan.md`,
`executor.md`, `pyproject.toml`, `uv.lock`, this handoff, existing tests and core code.
Propose dependency/shared-interface changes in stream-specific notes. A later
integration session reviews both proposals, updates shared interfaces once, then
runs the combined suite, migration compatibility checks, replay and a distinct
live check. Neither job merges into main or force-pushes the checkpoint branch.

Limits: strict aliases/rule wording and exact start-time equality can exclude valid
contracts; there is no finalized-market collection or settlement adjudication yet.
No forecasting, T−60 freezing, scoring, 24-hour endurance, automated retention,
sleep-gap monitoring or tested production restore policy exists. Fixture import
interrupted mid-record must be diagnosed; it now fails closed rather than claiming
completion. No arbitrary endpoint support, credentials, paid services or trades.
Filesystem loss remains a risk until the backup workstream is verified.

## Engineering skills and reversal

Installed Cool Coder engineering skills **1.2.0**, upstream commit
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. This checkpoint used `code-review`,
`reverse-branching`, and the data-systems-design, security-engineering and
systems-programming reference catalogs. All 50 files in these five installed
folders matched that pin; their hashes are [recorded](../checkpoint-skill-provenance.json).
No skill files were changed or tooling upgraded. Preserve any later local edits.

The checkpoint publishes code, not a running deployment. To abandon the draft,
close its PR and retain the commits/data. To undo a future published code change,
use a new revert commit on that job's branch; never reset or force-push shared
history. A code revert does not undo database writes or a future migration.
Stop a running collector before changing its version and preserve its archive.
No schema change or destructive cleanup is included in this checkpoint. Restoration
RPO/RTO and production rollback timing are **not measured** and are not claimed.
