# Milestone 1 verification — 2026-09-27

Verdict: milestone 1 acceptance criteria met. The implementation is ready for
manual one-shot collection and inspection on this Mac.

## Offline and package checks

| Check | Result |
|---|---|
| `.venv/bin/ruff check src tests` | Passed |
| `.venv/bin/pytest -q` | 93 passed in 3.17 seconds |
| `.venv/bin/uv build` | Source archive and wheel built |
| Packaged migration/command smoke check | Wheel includes SQL; packaged CLI initialized fresh schema 1 database |
| Fixture import and repeated replay | Stable normalized rows and retrieval identities |
| Process failure before/after commit | Raw data, derived records and checkpoint are atomic |
| Second writer process | Rejected by database-adjacent process lock |
| Offline HTTP isolation | Real HTTP transport blocked in pytest; fixture CLI commands make no network requests |

Focused coverage includes decimal and fractional representations; missing/null/empty
books; crossed/malformed levels; repeated bytes at distinct retrievals; pagination
limits/loops and invalid cursor restart; 429/5xx/timeouts/403; Retry-After budgets;
oversized/compressed responses; a continuously streaming response bounded by the
total deadline; ambiguous games, explicit doubleheaders, postponements, TBD times,
schedule freshness, exact cutoff boundaries, and resumed runs with only historical
books. A late book remains archived raw while normalization rejects it.

## Independent live integration

Command: `.venv/bin/vader --db data/live.sqlite3 live-check`.
The real Python client used public production Kalshi and official MLB endpoints.

| Evidence | Final run |
|---|---|
| Run ID | `312d248b-38a2-42dd-baeb-b1202389a886` |
| Started UTC | 2026-09-27 16:30:17.871456 |
| Finished UTC | 2026-09-27 16:30:39.145464 |
| Status / errors | Complete / none |
| Retrievals | 43 |
| Full books | 22 contracts across 11 distinct MLB games |
| Versioned entities / observations | 142 / 82 |
| Excluded candidates | 4 rule/schedule time mismatches; 2 unmapped; 2 not in accepted pregame status |
| Reopen checks | SHA-256, byte lengths, SQLite quick check and foreign keys passed |
| Timing inspection | All 22 books preceded cutoff and used schedule evidence aged 0–120 seconds at request start |

Both contract document URLs, series metadata, paginated market discovery, event
metadata and official schedule evidence were archived. The live database is at
`/Users/ashwin/Documents/github-repos/Vader-Intelligence/data/live.sqlite3` and is
excluded from Git. [Machine-readable evidence](live-check-evidence.json) records
the code hash and exact counts.

An earlier live run at 15:37 UTC also succeeded with 22 books. Its exclusions
included the then-unreviewed “Chicago WS” alias, subsequently matched against the
official game and added explicitly. In the final run that game's official status
was outside the accepted Scheduled/Pre-Game set, so its two books were excluded.
No ambiguous mapping or live status was overridden to increase coverage.

After both runs, replay processed all 86 responses with no errors and no changes
to counts: 82 unique bodies, 86 retrievals, 284 entity versions, 164 observations,
60 eligibility decisions. This demonstrates deduplicated bodies and distinct
retrievals, plus replay idempotence on actual provider responses.

Health immediately after verification reported fresh books, zero pending or
interrupted attempts, and approximately 10.9 GB free disk. The database occupied
2,105,344 bytes excluding its transient sidecar files. This small sample does not
establish daily storage growth; repeated full metadata snapshots add overhead.

## Remaining limits

Strict scope and mapping rules intentionally reduce coverage. Unknown aliases,
new rule wording, reschedules, uncertain doubleheaders, TBD starts and unmatched
times remain excluded until reviewed. MLB postseason listings/depth are not
guaranteed by this run. Market depth is an observation, not evidence of a fill.

No continuous collection, 24-hour endurance, sleep-gap monitoring, automated
backup restoration, settlement reconciliation, forecast scoring or predictive
advantage has been demonstrated. Those are milestones 2–4. A manual collection
ends after one pass; health becomes stale once its freshness window expires.
Source files are local and have not been committed or pushed.
