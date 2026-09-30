# Collection operations

The standalone entry point is `ops/manage.py`. It wraps the existing collector CLI
without changing storage, migrations, shared CLI code or settlement. Python 3.11+
and a local filesystem are required. Scheduling and public collection require
macOS/Unix; portable operations tests also run on Windows.

**Deployment status:** developed on Windows. No Mac service was installed or
started, and no fresh public collection was performed in this workstream.
Native launchd verification and the production archive restore drill remain Mac steps.

## Design and limits

The generated LaunchAgent uses absolute `ProgramArguments`, `WorkingDirectory`,
`RunAtLoad` and configurable `StartInterval`. It has no `KeepAlive` retry loop.
It runs in the logged-in user's GUI domain. It cannot collect during sleep, power
loss, logout or network unavailability. See Apple's
[LaunchAgent lifecycle and interval examples](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

Default cadence is **120 seconds**, with a **90-second process deadline** and
**5-second termination grace**. The baseline's actual live sample took about
17 seconds for 30 retrievals and 16 books across 8 games, with an 8-second request
budget, two attempts, three pages and 50 markets. `ops/collector.example.toml`
preserves those limits. At that sample size, 30/120 averages 0.25 retrievals/second;
the collector independently caps bursts at 2 requests/second. This sample is not
a worst-case estimate. The configured caps can reject larger universes, and slow
requests can exhaust the deadline. Those outcomes remain visible failures.

`--cadence` is configurable from 30 to 86400 seconds. The deadline plus grace must
fit within it. Before reducing cadence, inspect retained runtimes and API errors.
Multiple databases do not share the collector's rate limiter. The installer
conservatively refuses other detected Vader collectors, even for another database.

Overlap protection has three layers: launchd's single job instance, an adjacent
`.ops-run.lockfile`, and the collector's unchanged `.lockfile`. Lock files are never
unlinked. A canonical database path determines the service label and registration
path. Symlinks are canonicalized during configuration; hard-linked database files
are refused. Use a local disk, one operator account, and this lifecycle helper for
the production schedule.

Installation takes a per-database install lock and writes an exclusive adjacent
`.ops-registration.json`. The claim survives stopped jobs, bootstrapping failures
and crashes. It prevents a second managed schedule, including one with a test label.
The helper scans saved user/system plists, loaded GUI/user/system service definitions,
and the current user's crontab. Unknown inventory formats, unreadable definitions,
or possible collector jobs cause installation to stop before starting a service.
This does not prove absence of another host, another user's cron, or arbitrary
opaque scripts. The operator must account for those before deployment. Never bypass
a conflict by changing labels or deleting the registration/lock files.

## Verify the Mac checkout and tests

First deploy the reviewed operations commits to the stable collector checkout.
Do not schedule a temporary worktree. Do not switch another session's branch or
overwrite its changes. These commands assume integration has made the operations
files available in the existing stable checkout:

```sh
cd /Users/ashwin/Documents/github-repos/Vader-Intelligence
git status --short
git rev-parse HEAD
git merge-base --is-ancestor f0b0419ba45d1aeeb4ca663f6274981df09397d7 HEAD
.venv/bin/python --version
.venv/bin/uv sync --locked
.venv/bin/ruff check src tests ops
.venv/bin/pytest -q
.venv/bin/python -m unittest discover -s tests/ops -v

# Optional native drill: synthetic child, disposable DB, random com.vader.test label.
# Exercises a deadline failure, bootout, then successful empty discovery.
.venv/bin/python tests/ops/mac_launchd_smoke.py
```

The installer additionally checks a clean tracked checkout, baseline ancestry,
interpreter import location, collector configuration, database integrity and schema.
It refuses Git worktrees and OS temporary directories. Its script must itself be
running from the configured checkout. A venv interpreter symlink is intentionally
not resolved to the system Python.

## Configure and install

Review the paths below, particularly the production database, against the operator's
existing configuration. The handoff describes `data/live.sqlite3` as the historical
archive; it does not prove that is the currently selected production database.
Use new, dated configuration/state names when changing settings. The commands fail
instead of overwriting an existing operations config or state directory.

```sh
VADER_ROOT=/Users/ashwin/Documents/github-repos/Vader-Intelligence
VADER_PY="$VADER_ROOT/.venv/bin/python"
VADER_OPS="$VADER_ROOT/ops/manage.py"
VADER_DB="$VADER_ROOT/data/live.sqlite3"
VADER_CONFIG="$VADER_ROOT/data/collection-ops-20260928.json"
VADER_STATE="$VADER_ROOT/data/collection-ops-state-20260928"

"$VADER_PY" "$VADER_OPS" inspect --database "$VADER_DB"
"$VADER_PY" "$VADER_OPS" configure \
  --checkout "$VADER_ROOT" --python "$VADER_PY" \
  --database "$VADER_DB" \
  --collector-config "$VADER_ROOT/ops/collector.example.toml" \
  --state "$VADER_STATE" --output "$VADER_CONFIG" \
  --cadence 120 --deadline 90 --grace 5

# Inspect the exact definition before installation. This does not register a job.
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" plist
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" install
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" status
```

Installation immediately starts one bounded public read-only collection pass.
A loaded job is not proof of a successful pass. `status` may report `running` or
`never_invoked` initially; inspect it again after the configured deadline.
The installed plist is `~/Library/LaunchAgents/<label>.plist`. `configure` prints
the exact deterministic label. The registration records preflight evidence.
An operations-config SHA-256 guard rejects in-place edits during scheduled runs.

Do not store archives or backups inside the dedicated operations state directory.
Its `stdout-N.log`, `stderr-N.log`, `history.json` and `last-overlap.json` are reserved
operational files. Defaults retain 1000 invocations, approximately 33 hours at the
default cadence, with at most 64 KiB per stdout/stderr stream: about 125 MiB of log
payload plus bounded JSON history. Output beyond the cap is drained but discarded
and makes the invocation fail explicitly. Rotation only reuses these log slots.
It never deletes database rows, observations, source blobs or backups.

## Status, stop, restart and uninstall

With the variables above still set:

```sh
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" status
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" health
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" stop
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" restart
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" uninstall
```

Stop disables the label, boots it out, and waits for both operations and collector
locks to drain. It retains the plist and registration. Restart revalidates the
stable checkout and conflicts, enables the label, and bootstraps it. Uninstall
removes only the owned plist and registration after stopping. All archives, backups,
configurations, history and lock inodes remain. Stop/uninstall tolerate an already
unloaded job. They refuse a modified or unrelated plist.

For a cadence/configuration change: stop and uninstall using the old configuration,
create a new dated config/state directory, review its plist, and install it. Keep
the old configuration and history as change evidence. Restart refuses a changed
operations config. If bootstrap failed, inspect its error and run uninstall to
release the verified registration before retrying. Never delete claims blindly.

If the Python environment itself is broken, use the printed label out of band:

```sh
VADER_LABEL='PASTE_THE_EXACT_CONFIGURE_LABEL'
launchctl print "gui/$(id -u)/$VADER_LABEL"
launchctl disable "gui/$(id -u)/$VADER_LABEL"
launchctl bootout "gui/$(id -u)/$VADER_LABEL"
```

Restore the environment before using the normal helpers. Keep the registration
until child/lock state is verified. Do not kill an unrelated collector to clear it.

## Operational health and gaps

Each invocation reaching the runner records UTC start/end, monotonic runtime,
collector exit code, wrapper exit code, arguments, config hash, run ID and log paths.
The start record is durable before spawning. After an abrupt wrapper death, the
next pass retains an unknown completion rather than fabricating a successful end.
Logs for a slot belong to its recorded sequence and are replaced when that sequence
ages out. A start without completion is not proof that its log files are complete.

| Health status | Evidence and meaning |
|---|---|
| `collected_eligible_games` | Exit 0, complete JSON, no errors, positive books, matching completed `collect` run, positive eligibility count. |
| `discovery_no_eligible_games` | Exit 3, inconclusive JSON, no errors, zero new books, matching completed `collect` run, zero eligible rows. Successful discovery; no claim of live book coverage. |
| `inconclusive_with_eligible_games` | Empty books do not prove an empty eligible universe, for example a cutoff crossing. Needs inspection. |
| `failed_or_partial`, `wrapper_failed` | Collector failure/partial status, incompatible output, launch/I/O/verification failure. |
| `deadline_exceeded`, `interrupted` | Wrapper deadline or termination request; evidence already archived remains intact. |
| `running`, `stale_activity`, `completion_unknown` | Started, but completion has not been observed or the bounded activity window has elapsed. |
| `never_invoked`, `missed_or_stale`, `clock_regression` | Missing invocation evidence, start spacing beyond cadence plus grace, or wall-clock reversal. |
| `scheduler_failed` | `launchctl` reports a non-success exit or terminating signal, including failures before the runner started. |

The read-only queries in `ops/archive.py` verify schema 1, select the exact `runs.id`,
require kind `collect`, compare the persisted summary, and count that run's eligible
rows. Fixture runs cannot establish operational success. The core `vader health`
command remains unchanged and can report unhealthy because it requires fresh live
books. Operations health deliberately reports discovery/activity separately.

Wrapper exits: 0 collected; 3 inconclusive (including healthy empty discovery);
1 failure or unverifiable result; 75 overlap skipped; 124 deadline; 130 interrupted.
`health` exits 0 for either verified success category when activity is fresh and
there is no storage pressure. `status` additionally requires a loaded scheduler
without a reported failure. Other states exit 1. On Windows, scheduler status is
explicitly unknown, and `status` cannot return success.

History records gaps when the next invocation arrives; health also reports an open
gap while no new invocation arrives. This is evidence of missing collection activity,
not an exact count of lost snapshots. No missing snapshots are created or backfilled.
Gap cause is always unknown automatically. For supported attribution, correlate
the interval with the Mac power log and that run's archived request errors:

```sh
pmset -g log
sqlite3 -readonly "$VADER_DB" \
  "SELECT r.run_id,f.started_at,f.retrieved_at,f.state,f.error FROM fetches f JOIN requests r ON r.id=f.request_id ORDER BY f.seq DESC LIMIT 100;"
"$VADER_PY" "$VADER_OPS" --config "$VADER_CONFIG" status
```

A timeout by itself is not proof of network failure, and elapsed wall time is not
proof of sleep. Positive clock corrections can resemble gaps. Use corroborating
evidence before assigning a cause. If the wrapper cannot even load, use the raw
launchctl detail in `status`; run the wrapper manually after stopping the schedule
to expose configuration/interpreter errors.

`status` reports database/WAL/SHM sizes and free bytes. The operations warning uses
512 MiB, the baseline collector's default reserve. Custom collector reserves remain
independently enforced by the collector. There is no automatic archive retention or
paid monitoring. The operator owns inspection and disk/backup capacity.

## Backup and separate restoration

The implementation uses Python's SQLite online backup API with a read-only source,
not a raw file copy. This captures committed WAL content. See the
[SQLite backup documentation](https://www.sqlite.org/backup.html).
It verifies `user_version=1`, the migration record and expected table columns,
then reopens the destination for integrity and foreign-key checks and row counts.
Future schema versions are refused until integration reviews the expected schema.

Choose a new backup filename each time. Prefer an independently protected volume
for eventual disaster recovery. This example's adjacent backup is a local recovery
copy and cannot protect against loss of the Mac/disk. No backup deletion is automated.

```sh
mkdir -p "$VADER_ROOT/data/backups"
VADER_BACKUP="$VADER_ROOT/data/backups/live-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
"$VADER_PY" "$VADER_OPS" backup --source "$VADER_DB" --destination "$VADER_BACKUP"

VADER_RESTORE_DIR=$(mktemp -d /tmp/vader-restore.XXXXXX)
VADER_RESTORE="$VADER_RESTORE_DIR/restored.sqlite3"
"$VADER_PY" "$VADER_OPS" restore --source "$VADER_BACKUP" --destination "$VADER_RESTORE"
"$VADER_PY" "$VADER_OPS" inspect --database "$VADER_RESTORE"
sqlite3 -readonly "$VADER_RESTORE" \
  "SELECT id,kind,started_at,status FROM runs ORDER BY rowid DESC LIMIT 5;"
```

Restore uses the same supported API and requires a new destination. It cannot
overwrite the production database, another backup, an existing file or a symlink.
If backup/restore fails, the incomplete destination remains for diagnosis; never
reuse it. Only a successful result with `integrity: ok` is a verified copy. The
300-second backup-copy deadline does not include the subsequent full integrity scan.
Reported runtime includes verification. Large archives need a measured restore
drill; the tiny test archive timing is not a production RTO. Actual production RPO,
RTO, backup cadence, and off-host retention remain operator decisions.

## Later evidence and reversal

There is no need to keep a coding session open for an endurance test. After a day,
run the same `status` command and inspect `history_path`, `retained_invocations`,
`recorded_gaps`, runtime fields, exit codes and archive sizes. The default history
holds approximately 33 hours; preserve the report before its records rotate.
Do not claim continuous coverage if the computer slept or activity is missing.

The reversible artifact is the scheduled job and its versioned code/configuration.
The operator stops/uninstalls it with the commands above, then deploys a reviewed
revert commit if needed. Code reversal does not erase captured observations or
recall public GET requests. All archive data is retained. No destructive migration
or production restore is part of this procedure. Native stop/restart time and
production restoration time are unmeasured in this Windows session.
