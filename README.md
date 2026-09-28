# Vader Intelligence

Milestone 1: a read-only MLB prediction-market archive on this Mac. It collects
Kalshi `KXMLBGAME` full-game team winner contracts and full order books only when
fresh official MLB schedule evidence establishes that the game is pregame.

## Setup

Requires Python 3.11+ and macOS or another Unix system with `flock` and process
alarms. The checkout is `/Users/ashwin/Documents/github-repos/Vader-Intelligence`;
the synced ChatGPT project mirror is preserved separately.

```sh
cd /Users/ashwin/Documents/github-repos/Vader-Intelligence
python3 -m venv .venv
.venv/bin/python -m pip install 'uv==0.12.19'
.venv/bin/uv sync --locked
.venv/bin/vader db-init
```

The environment is already installed on this Mac. `uv.lock` pins all application
and development dependencies. No API key or paid account is required. Databases,
logs, environments, credentials and generated distributions are excluded from Git.
Do not place secrets in configuration; this program does not use them.

## Operate

Run from the checkout. Global options (`--config`, `--db`) precede the command.

```sh
# Discover metadata, contract documents, and official schedule eligibility.
.venv/bin/vader discover

# Collect one bounded pass, including full eligible books.
.venv/bin/vader collect --once

# Inspect freshness, incomplete attempts, and disk space without a writer lock.
.venv/bin/vader health

# Separately invoked real-endpoint integration check; then reopen and verify hashes.
.venv/bin/vader --db data/live.sqlite3 live-check

# Reprocess archived successful/malformed HTTP 200 responses, without HTTP requests.
.venv/bin/vader --db data/live.sqlite3 replay

# Resume the same unfinished command using the run_id from its summary or database.
.venv/bin/vader collect --once --resume RUN_ID
```

Every command prints a JSON result. Exit codes: `0` complete, `1` failed/partial or
unhealthy, `3` inconclusive, `130` interrupted by the operator. A live check with
no eligible games is inconclusive. `books` counts the whole run; `books_this_pass`
counts newly collected books in the current invocation, including on resume.
Two opposing contracts usually represent one game: book counts are not game counts.

An unfinished run can resume only with the original code hash, configuration and
command. After a code/configuration change, start a new run; the old evidence stays
archived. Discovery resumes committed pages, restarts once for an invalid cursor,
then refreshes current metadata and schedule before requesting any books. Missing
intervals are not backfilled. A second writer for the same database is rejected.
After an ungraceful termination, resume marks pending attempts interrupted.

Optional configuration:

```sh
cp config.example.toml config.local.toml
.venv/bin/vader --config config.local.toml collect --once
```

Paths in TOML are relative to the configuration file; `--db` paths are relative to
the current directory. Defaults enforce one request at a time, at most 2 requests/s,
5-second connect / 10-second read timeouts, at most 3 attempts in 45 seconds,
bounded payloads/pages/market counts, and a 512 MiB minimum free-space reserve.
429, 5xx and transient network failures receive bounded retries with jitter and
Retry-After handling. 401/403 stop collection. TLS verification stays enabled;
redirects, proxies from environment variables, and trading endpoints are unsupported.

## Archive and inspect

SQLite holds response bytes and derived records together. WAL, FULL synchronous
commits, foreign keys, process locks and explicit short transactions protect the
archive. Each actual retrieval has its own UUID and sequence; identical bytes share
a body hash but produce distinct observations. Replay uses retrieval/parser keys
to avoid duplicate normalized rows. Malformed and interrupted responses retain
failure evidence; partial bodies are flagged and never normalized.

```sh
sqlite3 -readonly data/live.sqlite3 "SELECT id,kind,started_at,status,summary_json FROM runs ORDER BY rowid DESC LIMIT 5;"
sqlite3 -readonly data/live.sqlite3 "SELECT ticker,reason,game_id,cutoff FROM eligibility ORDER BY id DESC LIMIT 30;"
sqlite3 -readonly data/live.sqlite3 "SELECT ticker,data_json FROM observations WHERE kind='book' ORDER BY id DESC LIMIT 2;"
sqlite3 -readonly data/live.sqlite3 "SELECT seq,id,status,state,error,body_sha256 FROM fetches ORDER BY seq DESC LIMIT 20;"
```

Join `observations.fetch_id → fetches.id → requests.id` via `fetches.request_id`
for URL/query/run provenance. Join `fetches.body_sha256 → blobs.sha256` for exact
bytes. Book JSON links an immutable `eligibility_id`, which links the source market
and official schedule retrievals. `entities` versions identifiers, hierarchy,
rules and all source fields. `runs.provenance_json` records source/configuration
versions; runtime and completion details live in `runs.summary_json`.

Prices and quantities remain decimal strings. YES ask is `1 − best NO bid`; NO
ask is `1 − best YES bid`, using the same returned book. Missing/null sides and
observed empty sides remain distinct. Full returned levels and fractional sizes
are retained, and crossed books are flagged. Displayed depth is not a promised fill.

The live evidence collected during implementation is in `data/live.sqlite3` on
this Mac. It is deliberately not part of Git. See [verification evidence](docs/verification.md).
Health becomes stale after 180 seconds without new live books; a successful
one-shot collection does not imply continuous collection is running.

## Offline verification

```sh
.venv/bin/ruff check src tests
.venv/bin/pytest -q
.venv/bin/uv build

# Use a separate database for explicitly synthetic fixtures.
.venv/bin/vader --db data/fixture.sqlite3 replay --fixture tests/fixtures/mlb.json
.venv/bin/vader --db data/fixture.sqlite3 replay
```

Tests block real HTTP transport and use deterministic synthetic responses. They
cover exact prices, malformed/missing/empty books, repeated bodies/replay,
pagination and expired cursors, retries/deadlines, crash boundaries, writer
contention, ambiguous/doubleheader/postponed games, cutoff timing and recovery.
Fixture books never count as fresh live data in health reports.

## Scope and limitations

Matching uses reviewed team aliases, exact rule date/time, official game identity
and game number. It excludes unknown aliases, ambiguity, changed start times,
postponements and TBD starts. Books stop two minutes before official first pitch;
Kalshi close/expiration times do not establish the start. This strict policy can
exclude valid contracts until mappings are reviewed. Rule wording/schema changes
also fail closed. Full future mapping and settlement adjudication are milestone 2.

Milestones 2–4 (settlement, T−60 baselines/evaluation, scheduled operation) remain
deferred. There is no scheduler, trading, forecast generation or model spending.
The versioned provenance interface reserves model/prompt/input/output/evaluation
fields, which are empty in collector runs. No claims of predictive edge are made.

All data is retained, with no automatic cleanup or backup schedule. The planning
estimate is roughly 0.35 GB/day at 30 books/minute, before measured overhead and
metadata growth. Monitor actual database and WAL sizes. To make a consistent
manual backup, stop writers and use SQLite's backup command (choose a new path):

```sh
sqlite3 data/live.sqlite3 ".backup 'data/manual-backup.sqlite3'"
sqlite3 -readonly data/manual-backup.sqlite3 "PRAGMA integrity_check;"
```

Do not copy only the database file while a writer may have committed data in WAL.
A tested restoration procedure and 24-hour operational validation belong to F4.

Architecture and future milestones: [plan](plan.md). Implementation contracts:
[executor](executor.md). Upstream setup provenance: [source audit](docs/source-audit.md).
