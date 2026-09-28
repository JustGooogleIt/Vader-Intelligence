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
- [ ] Test baseline migration, corrections, ambiguity, exceptional/missing payouts,
  repeated ingestion, interruption/replay and existing collector behavior.
- [ ] Run independent bounded live settlement check, review diff, commit and draft PR.

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
Next: implement schema/models, then refresh and tests in this worktree only.
Verification/results and migration instructions will be appended in coherent commits.

Implementation checkpoint: schema, mapping, provider models, bounded refresh with
resume, inspection, and replay are implemented. Initial full offline suite: **140
passed** (network forbidden); lint passed. Synthetic fixtures are labelled in tests.
First separate live run `6ce90956-853e-4851-9e12-a714b2b95df7` completed against
`KXMLBGAME-26SEP261915CHCBOS-CHC`; all five public retrievals succeeded, but mapping
was correctly quarantined because the current MLB schedule did not establish the
original identity. This is evidence of live archival, not a successful game match.
Remaining: deeper review/edge cases, final build/CLI verification, live archive
reopen/hash/replay evidence, operator documentation and draft PR.
