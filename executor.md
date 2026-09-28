# Implementation specification

## Phase F1: MLB discovery and collection

The accepted plan is sufficient authorization to detail and implement this phase without
another approval round. Milestones 2–4 are not implemented.

### Components and ordered steps
1. `config.py`, `provenance.py`, CLI: TOML settings, immutable RunProvenanceV1 contract,
   local process lock, db-init/discover/collect --once/replay/health/live-check.
2. `storage.py`, `migrations/001_initial.sql`: SQLite archive and atomic normalized writes.
3. `transport.py`: GET-only HTTP requests with fixed hosts/routes and archive every attempt.
4. `normalize.py`, `scope.py`: exact decimal parsing, versioned entities/books, reviewed
   MLB aliases, deterministic event identity and official pregame eligibility.
5. `collector.py`: resumable bounded pagination, series/documents/event/schedule/market
   collection and eligibility-rechecked books. CLI wiring, fixture import and replay.
6. Tests, runbook and explicit live-check evidence. Update this spec with verification.

### Data and public interfaces
CLI emits JSON summaries and nonzero on failed/partial/inconclusive collection.
`replay --fixture PATH` imports a bounded archive manifest with fixed retrieval IDs;
`replay` reprocesses saved responses without network or new retrievals.
SQLite: schema_migrations; runs; blobs keyed by SHA-256; requests (logical request,
run, stage, URL/query, done and cursor checkpoint); fetches (attempt sequence, UUID,
status, body hash, timing, parse/transport error); entities (fetch/kind/ticker/parser);
observations (fetch/ticker/kind/parser, exact-string JSON); eligibility (run/market,
game ID, reason, schedule fetch reference, cutoff). Immutable source records never update.
Requests/checkpoints and run states are mutable operational state.
Access paths: request resume by run/stage/key; entity/observation by fetch and ticker;
latest run and last successful book; streaming replay in bounded batches.

### Invariants and contracts
- Every HTTP attempt gets a UUID and a pending fetch row before network; completed body,
  parsed rows and successful request checkpoint commit in one BEGIN IMMEDIATE transaction.
  Pending attempts after crash become interrupted; retry creates a new retrieval identity.
- A UNIQUE(fetch_id, kind, ticker, parser_version) constraint makes replay idempotent.
  Equal hashes at different retrievals remain distinct observations. Raw body hash is over
  raw response bytes with identity encoding requested, not reserialized JSON. Unknown
  JSON fields remain in raw bytes; unexpected content encodings are archived and rejected.
- WAL/FULL/foreign_keys and a 5s SQLite busy timeout; process-level nonblocking flock on
  a stable adjacent file protects all writer commands. No distributed lease is needed.
- Parsing failure archives raw data, with a savepoint rolling back partial normalization.
  Cursors are opaque; repeated cursor, >100 pages, >100 markets, or oversized response
  marks incomplete. Expired cursor HTTP 400 resets discovery once, preserving observations.
- HTTP defaults: 2 requests/s, concurrency 1, connect 5s/read 10s, three attempts total,
  45s monotonic total deadline, base 0.5s capped exponential full jitter, Retry-After honored
  within budget. Retry network failures/429/5xx only. 401/403 abort the run. No redirects.
  A Unix main-thread alarm interrupts a hanging stream at the monotonic total deadline; partial bytes are
  marked truncated, never normalized. JSON response cap 2 MiB; PDF cap 10 MiB.
  Request identity encoding and bound raw streaming bytes; unexpected compressed bodies
  are archived encoded with their Content-Encoding header and rejected, never decompressed.
- Sources: external-api.kalshi.com/trade-api/v2, statsapi.mlb.com/api/v1/schedule,
  assets.kalshi.com contract_terms and regulatory/product-certifications PDFs only.
  HTTPS verified, no credentials, no arbitrary endpoint/config host, no writes to providers.
- Parse dollar prices and fixed-point quantities from finite nonnegative decimal strings;
  dollar prices <=1, quantities fractional. Preserve missing versus null versus empty arrays.
  Full `orderbook_fp` arrays retained; infer asks only from the same response, not market
  listing quotes. Crossed books are flagged rather than clamped. Only $1 binary contracts.
- Reviewed static MLB alias/code table resolves canonical IDs. Match original event date,
  exact participants, rule-stated local time (America/New_York), and game number where given.
  Require an ordinary full-game team-win rule, exactly two opposing contracts, known
  league/scope, unambiguous official game, non-TBD Scheduled/Pre-Game status, future cutoff,
  no postponement/reschedule markers, and schedule fetched <=120s ago. Unknowns fail closed.
  Stop requests at first pitch minus 120s; refresh schedule before it becomes stale.
- At resume, refresh metadata/schedule and re-evaluate eligibility before every book;
  never resume a stale historical book queue blindly. No fabricated backfill during gaps.
- Retain all raw data this sprint; no automatic deletion. Stop writes at <512 MiB disk free;
  health reports free space and stale/partial data. Committed data survives process crash;
  local-disk loss is accepted until a separately verified backup policy in F4.

### Failure modes / hazard and security scan
| Failure / hazard | Mechanism |
|---|---|
| H-03/H-14 duplicate retry or replay | SQLite unique constraints, immutable retrieval identity |
| H-11/H-32 dropped data / dual write | Raw bytes and derived rows in one database transaction |
| H-15/H-16/H-17/S-47 outage or hung read | One bounded retry layer, monotonic deadline, Unix alarm |
| H-20 clock change | Sequence ordering; injected monotonic durations; UTC evidence only |
| H-33/V-62 huge body/page/book | Streaming byte caps, bounded pagination and market counts |
| S-07/S-11 crash, partial commit | SQLite FULL transaction and recovery tests |
| V-32/V-59 malicious provider/input | Verified TLS, fixed host/routes, parameterized SQL, no shell |
| V-14/V-58 leaked credentials | No credential inputs, allowlisted metadata and sanitized errors |
| 401/403 | Archive response, fail closed; no access-control bypass |
| Missing/stale/ambiguous schedule | Archive reason and skip books |
| Book response crosses cutoff | Preserve raw, reject as pregame observation |
| Interrupted page | Resume request; expired cursor restarts once; no lost prior archive |

### Observability and verification
JSON summaries: run ID, status, requests/fetches, entities, market/book observations,
eligible/excluded counts and reasons, errors, elapsed time. Health: latest run state,
last book time/age, pending/interrupted fetches, disk free, archive size.
No external alerting/scheduler is installed. Empty eligible live universe is inconclusive.
Tests cover offline manifest repeatability, page/cursor errors, decimal/empty/missing data,
same bytes at different times, crash boundaries, process contention, timeout/429/5xx/403,
oversize/invalid data, unknown aliases/doubleheaders/postponements/TBD/start boundaries.
Live-check must independently archive >=1 eligible book plus metadata, rule documents and
official schedule, close/reopen the DB and verify raw hashes, references and pregame timing.
Build package and run Ruff and pytest. Network is forbidden in offline fixtures/tests.

### Rollback and compatibility
Initial additive schema version 1. Future schema versions are refused by this executable.
Stop collector; preserve DB and source bytes; replay derivations after parser fixes under a
new parser version. Never remove raw data to roll back. No existing application to migrate.
No paid calls or trades exist. Human operator retains local control of database/files.

### Future enhancement notes
Official outcome adjudication, baseline forecasts/evaluation, scheduling/tmux supervision,
backup automation, agents and learning/promotions remain out of scope.

### Verification — completed 2026-09-27

93 offline tests passed (3.17 seconds), Ruff passed, source and wheel builds passed.
The wheel contains its SQL migration and successfully initialized a fresh database
when loaded directly from the built distribution. The production Python client
completed run `312d248b-38a2-42dd-baeb-b1202389a886`: 43 retrievals, 22 full books
across 11 games, no errors. Hashes/references verified after reopening; all 22
books had fresh schedule evidence and arrived before cutoff. Replaying 86 archived
responses created no duplicate rows. See `docs/verification.md` and the machine-readable
`docs/live-check-evidence.json` for timestamps, counts and source hash.

Verification found and fixed a resumed-run reporting error (historical books could
mask an empty current pass), incomplete-response flags, and the reviewed Chicago WS
alias. Regression coverage includes interrupted resume with no currently eligible
games and late raw-only book responses. No acceptance-blocking discrepancy remains
for F1. Operational endurance, settlement and evaluation are deliberately unverified
until their respective milestones.
