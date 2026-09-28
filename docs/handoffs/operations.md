# Collection operations handoff

## Baseline and scope

- Baseline: `f0b0419ba45d1aeeb4ca663f6274981df09397d7`.
- Branch: `feat/collection-ops`, isolated clone on Windows, 2026-09-28.
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

Default cadence proposal: 120 seconds, deadline 90 seconds, termination grace 5 seconds.
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
Use their applicable failure, reversal, data hazard and security catalogs in review.
Shared skill installations are untouched.

## Progress

- Read baseline, AGENTS.md, README, collector CLI, configuration, collector and schema.
- Verified the branch starts at the exact requested baseline.
- Windows has no WSL installation; native launchd validation must run later on Mac.
- Implementation and verification in progress.

## Integration requests

To be finalized after interface verification. No shared changes made.

## Deployment and reversal

Scheduler status: not installed or running by this session; Mac status unverified.
The operator must deploy reviewed code to the stable Mac checkout before installation.
Stop/uninstall the agent to reverse scheduling; retain all archive and backup files.
Code reversal does not undo collected observations. Production RPO/RTO are unmeasured.
