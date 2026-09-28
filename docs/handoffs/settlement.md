# Settlement workstream

Baseline: `04c57eed37261b8ed94b27cc325699eb2ad456f7` (resolved from the preceding
verified checkpoint; the request's `BASELINE_SHA` was a placeholder).
Branch: `feat/settlement-v1`. Isolated worktree:
`/Users/ashwin/Documents/github-repos/Vader-Intelligence-settlement-v1`.

## Progress and plan

- [x] Read AGENTS, baseline handoff, plan, executor and relevant source.
- [x] Create isolated worktree/environment; confirm private remote.
- [x] Verify current Kalshi schema/lifecycle, baseball terms and live response shapes.
- [x] Add explicit schema-2 migration and append-only evidence/version tables.
- [x] Implement conservative mapping and independent provider outcome models.
- [x] Implement bounded refresh, replay, inspection and minimal CLI integration.
- [x] Test baseline migration, corrections, ambiguity, exceptional/missing payouts,
  repeated ingestion, interruption/replay and existing collector behavior.
- [x] Run independent bounded live settlement check, review diff, commit and draft PR.

## Implementation specification (F2)

This task's explicit authorization includes settlement CLI/migration integration;
it supersedes the earlier instruction to defer those edits. The F2 spec is kept
here, per the requested ownership, rather than editing shared plan/executor files.

Ordered components: additive `migrations/002_settlement.sql`; `settlement/schema.py`;
`mapping/` for exact team/date/time/game-number mapping; `settlement/models.py` for
separate Kalshi and MLB interpretations; `settlement/journal.py` for version/evidence
insertion; `settlement/service.py` for refresh/replay/inspection; settlement CLI
dispatch from the shared CLI. Reuse the existing bounded Reader and Store.

The migration is explicit (`vader settlement migrate`), transactional and additive.
Normal `db-init` continues to create baseline schema 1. New code supports schemas
1 and 2; settlement commands require 2. Old checkpoint binaries refuse schema 2;
rollback requires preserving the expanded database and using compatible code or
a separately tested backup, never dropping evidence. Existing table shapes stay intact.

Version tables retain immutable semantic revisions with previous-version links;
observation tables link every actual retrieval to the version observed. Identical
semantic results reuse a version but never suppress a new retrieval. Changed payouts
or results append revisions; they cannot overwrite prior evidence. Reprocessing a
retrieval is deduplicated by database constraints in the same BEGIN IMMEDIATE
transaction. Ordering uses ingestion sequence, not provider clocks. Source timestamps
and retrieval times remain separate; missing fields remain missing.

Per-run target checkpoints reference market, event, schedule and contract-document
retrievals. Responses and target progress commit together. Final mapping joins only
archived evidence; no network inside a write transaction. Replay processes retrievals
in sequence, with bounded batches, and preserves successful materializations.

Kalshi lifecycle and payout category are separate: pending/determined/disputed/amended
are not paid-out results; REST finalized denotes completed settlement. `amended` is
a provider correction before payout. A changed observed payout/result after a prior
version is recorded as an observed correction, not an invented provider revision ID.
YES payout comes only from `settlement_value_dollars`; $1 minus YES is explicitly
derived NO payout. Scalar/fractional payouts never become binary outcomes. MLB final
results never manufacture or overwrite Kalshi settlement, including later corrections.

Mapping uses reviewed aliases and the exact full-game rule. Ordinary matches resolve;
explicit game number or unique original start disambiguates doubleheaders. Official
reschedule links or previously evidenced stable game IDs support changed schedules.
Unresolved identity, reversed teams, unknown rules and unsupported changes quarantine
with candidate IDs/reasons. Cancellation/postponement is retained as MLB state and
does not imply any Kalshi payout. Exchange fair-price discretion is never emulated.

Bounds: reuse existing one-flight 2 requests/sec, 3 attempts/45 seconds maximum,
TLS/allowlist/byte caps; refresh additionally bounds targets/pages and total runtime.
401/403 abort. Other errors report partial/failed with retrieval IDs. No trading,
forecasting, paid services, scheduler changes or live-database migration.

Hazards: H-14 unique retrieval observation keys; H-32 one SQLite transaction;
H-15/16/17 one bounded retry layer; H-20 sequence ordering; H-33 bounded requests and
inspection; S-07 SQLite FULL; V-32 TLS; V-59 fixed endpoints/parameterized SQL;
R-26 additive migration only, schema guard and explicit rollback limits.

## Skills

Cool Coder engineering skills 1.2.0, pin
`e46e79805be0ee5877fa9bc993492064bbb40aa5`: detail-planning, implement,
data-systems-design, verify, code-review, reverse-branching; applicable reference
catalogs from security-engineering and systems-programming. Installed skills are
used read-only; no shared installation is modified.

## Commands / next steps

Environment: `python3 -m venv .venv`, then
`.venv/bin/python -m pip install 'uv==0.12.19'` and `.venv/bin/uv sync --locked`.
Operator commands and migration/rollback boundaries are in [settlement.md](../settlement.md).

Implementation checkpoint: schema, mapping, provider models, bounded refresh with
resume, inspection, and replay are implemented. Initial full offline suite: **140
passed** (network forbidden); lint passed. Synthetic fixtures are labelled in tests.
First separate live run `6ce90956-853e-4851-9e12-a714b2b95df7` completed against
`KXMLBGAME-26SEP261915CHCBOS-CHC`; all five public retrievals succeeded, but mapping
was correctly quarantined because the current MLB schedule did not establish the
original identity. This is evidence of live archival, not a successful game match.
Remaining: deeper review/edge cases, final build/CLI verification, live archive
reopen/hash/replay evidence, operator documentation and draft PR.

## Final implementation and integration notes

Schema **2**, parser/mapping policy **1**. Three additive tables partition provider
versions, retrieval observations and target/evidence checkpoints. Separate physical
MLB/Kalshi tables were unnecessary; the constrained `kind` namespace keeps their
records independent in one append-only journal. No dependency or lockfile changes.

Important interfaces:

| Interface | Responsibility |
|---|---|
| `mapping.rule_identity`, `mapping.resolve` | Full-game/doubleheader rule and conservative official identity |
| `settlement.models.kalshi_result`, `mlb_result` | Independent lifecycle/payout/result projections |
| `settlement.journal.record`, `normalizer`, `map_target` | Revisions, idempotent observations, atomic evidence joins |
| `settlement.schema.migrate` | Explicit transactional schema 1 → 2 expansion |
| `settlement.service.SettlementRefresh.run` | Bounded public refresh and identical-input resume |
| `settlement.service.inspect` | Bounded latest/history inspection |
| `migrations/002_settlement.sql` | Additive tables, foreign keys, revision/observation uniqueness |

Minimal shared integration changes, authorized by the milestone-2 request:

- `cli.py` registers/dispatches the separate settlement CLI and reports the actual
  schema version from `db-init`.
- `storage.py` accepts schemas 1/2 and excludes settlement runs from the collector's
  latest-run health calculation. It does not auto-migrate a baseline database to 2.
- `transport.py` permits only the additional public MLB market-detail GET path.
- `replay.py` dispatches settlement stages using their persisted target keys.
- The existing future-schema rejection test now uses unsupported version 3.

No changes to `ops/`, `tests/ops/`, `docs/operations.md`, the live database,
scheduler, baseline handoff, root plan/executor, dependencies or shared skills.
Scheduler status from this workstream: none installed or enabled; the independent
operations session's scheduler is neither inspected nor controlled here.

Operations integration: keep using the existing collector CLI. Do not migrate its
database until both branches are reviewed together and compatible code is selected.
The old checkpoint refuses schema 2. Both collectors must not run simultaneously
against the provider merely because their databases are separate: rate limits are
per reader/process. `settlement inspect` is read-only; refresh/replay/migrate acquire
the same per-database writer lock as collection. No shared schema change is requested
from the operations session.

## Executed verification

On 2026-09-28, in this isolated worktree:

| Check | Actual result |
|---|---|
| `.venv/bin/ruff check src tests` | Pass |
| `.venv/bin/ruff format --check src tests` | Pass; 24 Python files |
| `.venv/bin/pytest -q` | **150 passed**, 6.27 seconds; network forbidden |
| `.venv/bin/uv build` | Wheel and source distribution built successfully |
| Wheel archive inspection | Settlement CLI, mapping module and schema-2 SQL present |
| Fresh temporary DB CLI | `db-init` → 1, migrate → 2, repeated db-init → 2, inspect/replay succeed |
| Empty DB health CLI | Correctly unhealthy, exit 1 |
| Live archive reopen | Every stored body SHA/length and SQLite/FK integrity verified |
| Live replay twice | Both complete; retrieval/version/observation counts unchanged |

The initial no-isolation build failed because hatchling was not installed in the
runtime environment. The supported isolated `uv build` succeeded without changing
project dependencies. An initial format check identified one new CLI line; it was
formatted before the passing checks above. No failed check is counted as a pass.

All **53 installed files** across the eight applied skills matched the pinned
upstream archive on a read-only comparison; no installation was changed.

### Separate live evidence

Run `4a9b18f3-7217-404f-b7a5-9ebfda033a34`,
**2026-09-28 16:18:00.648946–16:18:05.779282 UTC**, used the real Reader against
Kalshi and MLB. Source hash:
`e4307cc68a97c82361dbcbd069532c0af9177364fdaf108f2ce041d67a6242c4`.
Subsequent source changes only tightened configured limits and made CLI inspection
use one read snapshot. Final offline/build checks include those changes.

```sh
.venv/bin/vader --db data/settlement-final-live.sqlite3 settlement migrate
.venv/bin/vader --db data/settlement-final-live.sqlite3 settlement refresh \
  --ticker KXMLBGAME-26SEP251305CHCBOSG1-CHC \
  --ticker KXMLBGAME-26SEP271520BALNYY-NYY \
  --ticker KXMLBGAME-26SEP271505LADSF-LAD --budget 90
.venv/bin/vader --db data/settlement-final-live.sqlite3 settlement inspect --limit 3
.venv/bin/vader --db data/settlement-final-live.sqlite3 settlement replay
```

| Contract | Provider payout | Mapping evidence |
|---|---|---|
| `KXMLBGAME-26SEP251305CHCBOSG1-CHC` | Finalized NO, YES payout `0.0000` | MLB **824703**, verified doubleheader game 1 |
| `KXMLBGAME-26SEP271505LADSF-LAD` | Finalized YES, YES payout `1.0000` | MLB **823164**, exact original start |
| `KXMLBGAME-26SEP271520BALNYY-NYY` | Finalized scalar, YES payout `0.5300` | Quarantined `unverified_schedule_identity`; **no binary outcome** |

All 11 retrievals succeeded; 10 unique bodies, 102 semantic versions (including
MLB games in schedule windows), 224 observations and three target records. Reopen
and two offline replays preserved those counts exactly. Evidence database and
downloaded documents are ignored local artifacts, not committed fixtures.

The earlier three-target run `b3af3a2b-3f3a-41d7-9b19-9cb058e57c30` was **partial**:
MLB returned a repeated game ID in a schedule window. That exposed and prompted
the now-tested variant handling. The final fresh run above passed. Both earlier
local databases are retained; failed evidence was not replaced with fixtures.

## Review and acceptance evidence

Scoped review: changes since baseline, including the four shared integration files,
schema, owned modules/tests and documentation. Operations files excluded because
they are outside this stream. User authorization includes fixing review findings.

| Requirement / hazard | Evidence |
|---|---|
| Exact payouts and lifecycle | `settlement/models.py:31`; payout and pending-state tests |
| Mapping and quarantines | `mapping/__init__.py:49`; ordinary/doubleheader/change/ambiguity tests |
| H-14 duplicate effects | `settlement/journal.py:26`; unique observation key, repeated replay tests |
| H-32 partial multi-record write | `storage.py:160`; normalization savepoint and interrupted transaction tests |
| H-15/16/17 bounded calls/retries | `settlement/service.py:26`, `transport.py:123`; deadline/403/cursor tests |
| H-20 ordering | Ingestion sequence + retained source timestamps; A→B→A revision tests |
| S-07 durability, writer exclusion | Existing FULL/WAL/flock reused; complete baseline lock/recovery tests pass |
| R-26 migration compatibility | `settlement/schema.py:11`; DDL rollback and baseline book-preservation tests |
| V-32 TLS, V-59 input boundary | Fixed allowlisted GET paths, bounded bytes, parameters bound in SQL |
| Raw archive/replay | `replay.py:17`; hash verification and isolated ordered-rebuild test |

Review findings fixed: actual doubleheader wording was initially unsupported;
repeated MLB game IDs initially rejected unrelated games; unknown-rule quarantine
needed atomic event progress; inspection needed a consistent read snapshot.
Regression coverage and passing validation above substantiate the fixes.
No remaining material code defect identified in this scoped review. Final staged
diff check and all 44 tracked-file scans passed: no forbidden artifacts or matching
credential patterns. The ownership exclusion check passed. No unrelated refactor,
environment, database, downloaded provider payload or build output was committed.

Limitations: conflicting same-ID schedule variants stay ambiguous; there is no
automatic cross-window reschedule search or manual override workflow. Latest
inspection is ingestion-ordered; historical source-clock regressions remain
inspectable rather than adjudicated. Historical as-of joins, production restore,
24-hour operations, protected evaluation and forecasts are outside this milestone.
Live correction arrival was not observed; correction tests use labelled synthetic
evidence. No claim is made to have reproduced every exceptional payout mechanism.

## Publication and next steps

Before publication, authenticated GitHub access unexpectedly reported public
visibility. Applied the user's authorized privacy change and verified
`private=true`, `visibility=private` before pushing any settlement commits.
Remote `main` already exists; it is not changed by this workstream.

Published `feat/settlement-v1` without force. [Draft PR #2](https://github.com/JustGooogleIt/Vader-Intelligence/pull/2)
targets `checkpoint/milestone-1-reviewed`; main remains untouched. Implementation
and verification checkpoint: `78329cddd9357d340ce3fea0989521e35235c444`; the following
documentation-only commit records publication. The final branch SHA is returned to
the operator and can be verified with `git rev-parse HEAD` and the PR head SHA.

Next: review that PR with the operations branch in a later integration
task, run the combined suite and backup/migration checks, then choose deployment
and scheduling explicitly. No merge is authorized or performed here.
