# Code Review: review-uncommitted — milestone 1 checkpoint

Reviewed 2026-09-28 against the actual source, tests, SQLite evidence and authenticated
GitHub state. The operator explicitly authorized material fixes and publication.

## Summary

Exact prices/book complements, conservative scope, source identity, transactional
normalization, replay and lock behavior were inspected. Six failure-path issues
were fixed with regression tests before publishing the shared baseline. Schema,
parser and mapping versions remain 1; the changes preserve existing derived shapes.

## What was reviewed

- Entire initial implementation: 10 Python source modules, one SQL migration,
  six Python test files and one synthetic fixture; CLI, TOML, lockfile and docs.
- Initial feature checkpoint `48fdc7b` (28 files / 3,378 added lines including
  documentation and dependency lock), followed by focused review fixes.
- Existing live database read-only; integrity/replay exercised on a disposable
  SQLite backup. The operator's historical archive was not rewritten.
- Exclusions: future settlement, evaluation and 24-hour operations; unrelated
  repositories, local skill customization and general tooling upgrades.

## Critical issues found and resolved

| Location after fix | Finding and consequence | Resolution / evidence |
|---|---|---|
| `collector.py:185`, `transport.py:149` | Eligibility was checked once before the retry loop. Waiting/backoff or host sleep could send another book request with stale schedule evidence or after the cutoff, even if normalization later rejected it. | Recheck UTC cutoff and configured schedule age before every attempt and immediately before send. Two regressions prove no second HTTP call occurs after either condition expires. |
| `replay.py:68` | A repeated fixture import skipped existing bytes regardless of prior parse failure or changed source identity, allowing a failed import to claim success or mislabel provenance. | Require identical body/run/stage/URL/query/timestamps and successful original state; failed records remain failed. Re-import/provenance collision tests cover this. |
| `cli.py:72` | Reopen integrity errors changed only a complete live result to failed. With zero eligible books, corrupt archives could remain inconclusive. | Integrity errors always force failure. Empty healthy universes stay inconclusive without invented missing-book errors; both paths tested. |

No critical issues remain open within this checkpoint's scope.

## Medium-risk issues found and resolved

| Location after fix | Finding and consequence | Resolution / evidence |
|---|---|---|
| `normalize.py:178`, `scope.py:77` | Null/nonsensical nested schedule or eligibility shapes could raise unhandled attribute errors; a malformed schedule could roll back the raw response as well as normalization. | Validate date objects and nested eligibility metadata/status/time. Tests prove malformed schedule bytes remain archived and invalid eligibility shapes are excluded. |
| `storage.py:265` | A later synthetic fixture run could replace the live run in health's latest-run field and hide a recent collection failure. | Exclude fixture runs from both live run and book freshness queries; regression preserves latest live failure. |
| `transport.py:170` | The network alarm reused a remaining budget calculated before persistence; a slow pending-write could exceed the total request budget and shift rate limiting away from actual sends. | Recompute remaining budget and set the next-send limit immediately before network I/O. A delayed-persistence regression proves no HTTP call after budget exhaustion. |

No medium-risk checkpoint defect remains open. The current fixture import is not a
general resumable bulk-ingestion tool; interrupted fixture records fail closed and
require diagnosis. Production restoration/endurance work is explicitly deferred.

## Nits / polish

The build backend is not fully locked with all build-only transitive dependencies.
`uv.lock` locks application/development dependencies, and the tested toolchain is
recorded below. Bit-for-bit artifact reproducibility is not asserted. No unrelated
tooling upgrade was made to broaden this checkpoint into a build-infrastructure task.

## Hazard, failure and vulnerability scan

Source of truth: one SQLite database contains both raw provider bytes and derived
records; no network I/O occurs inside normalization transactions. The archive is
local research evidence, not a tamper-proof audit log against a malicious local owner.

| Catalog | Mechanism inspected / result |
|---|---|
| H-14 duplicate retry | Body hashes deduplicate bytes, never retrievals; UUID/sequence per attempt and unique normalization keys. Fixture identity enforcement strengthened. |
| H-15/H-16/H-17, S-47 | One retry layer, explicit connect/read/total deadlines, jitter and Retry-After; total budget includes pending-write time. |
| H-20 | Sequence numbers order ingestion; monotonic time controls elapsed/retries; UTC cutoff rechecked before sends. |
| H-32, S-07/S-11 | Raw bytes, derived rows and checkpoint commit together under BEGIN IMMEDIATE; WAL/FULL, foreign keys, savepoint rollback; process-exit tests before/after commit. |
| H-33, V-62 | Bounded raw streaming, pages/markets/levels, no unbounded decompression; terminal errors preserve incomplete evidence. |
| V-14/V-58 | No provider credentials; staged/history content and filenames inspected for secrets/artifacts; only selected response headers archived. |
| V-32/V-59 | Verified HTTPS, fixed host/path allowlist, redirects disabled, parameterized SQL; no provider input reaches shell execution. |
| V-64 | Ambiguous/stale/malformed eligibility fails closed. Integrity errors cannot be concealed by an inconclusive live status. |
| R-11/R-14/R-40 | Minimal main plus feature commits; local pre-fix checkpoint exists; no force push/history rewrite; independent worktrees and databases required for the two jobs. |
| R-48 | Current schema guard refuses later versions. New migration integration belongs to the settlement/integration workstream. |

## Scope check

- All changes address the requested checkpoint, material failure paths or handoff.
- No trade endpoints, model spending, scheduler deployment or new schema introduced.
- Private visibility is verified through authenticated GitHub access before pushing.
- Main contains only project title and `.gitignore`; implementation stays on the
  feature branch and draft PR. No merge into main.

## Validation actually performed

| Check | Result |
|---|---|
| `.venv/bin/ruff check src tests` | Passed |
| `.venv/bin/pytest -q` | 106 passed in 4.10 seconds |
| `.venv/bin/uv build` | Wheel and source archive built |
| Fresh database CLI | db-init, fixture import, replay returned 0; fixture-only health correctly returned 1/unhealthy |
| Wheel CLI smoke | Packaged CLI loaded its bundled migration and initialized schema 1 |
| Historical evidence integrity/replay | Disposable backup: 86 responses processed, no errors, unchanged counts (82 bodies / 86 fetches / 284 entities / 164 observations) |
| Fresh live check | Real public endpoints; 16 books / 8 games, hash verification after reopen; exact timestamps/source hash in `checkpoint-live-evidence.json` |
| Scheduler inspection | No repository scheduler or Vader launchd job/plist found on this Mac |
| Skill audit | 50 files across the five applied skill folders match pinned 1.2.0; none changed |

Offline tests block real HTTP transport and use synthetic fixtures. Live checks
run separately. A later no-eligible-games result must be recorded as inconclusive.
The live run used a fresh ignored database and a 90-second outer cap, 8-second
request budget, two attempts, three pages and 50 markets. Both live and historical
bytes remain local; only small verification summaries are committed.

Toolchain: Python 3.11.6, uv 0.12.19; existing build cache used Hatchling 1.32.4.
No dependency lock changes or skill/tool upgrades were made.

## Missing validation

No CI pipeline is configured. This report describes local execution, not hosted CI.
Twenty-four-hour operation, production backup restore, settlement correctness and
forecast evaluation remain untested future work, with explicit ownership in the handoff.

## Recommended next steps

Start both jobs from the final pushed checkpoint SHA with separate worktrees and
databases. Follow `docs/handoffs/baseline.md`; record shared-interface proposals
in stream-specific integration notes, then integrate and rerun combined checks.

## Verdict: READY WITH MINOR FIXES

Safe as the requested draft checkpoint. No material review defect remains; the
build-reproducibility caveat and future operational validation are documented.
This is not an approval to deploy a scheduler or merge the draft into main.
