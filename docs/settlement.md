# Settlement and event mapping v1

Milestone 2 archives **two independent providers**. MLB supplies game identity,
schedule and official results. Kalshi supplies contract status, result and payout.
An MLB winner never sets a Kalshi outcome. Collection is public HTTPS GET only.

## Setup and migration

Use this branch in its own worktree/environment and an isolated database. From the
repository root, with Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'uv==0.12.19'
.venv/bin/uv sync --locked
.venv/bin/vader --db data/settlement.sqlite3 db-init
.venv/bin/vader --db data/settlement.sqlite3 settlement migrate
```

`db-init` creates baseline schema **1**. `settlement migrate` explicitly adds schema
**2**, under the existing writer lock, in one transaction. Repeating it is harmless.
It adds `settlement_versions`, `settlement_observations`, `settlement_targets`, and
indexes. No baseline table is changed or backfilled. Baseline archived books remain
replayable. New code can collect against schema 1 or 2; settlement requires 2.

Do not apply this command to the live collector database during parallel work.
For later integration, stop overlapping writers, take and verify a SQLite backup,
test migration on a copy, and deploy compatible code before migrating the chosen
database. The checkpoint binary **rejects schema 2**. Reverting source does not
downgrade a database: retain the expanded archive and use compatible code, or
restore a separately verified backup to a different path. Never drop evidence or
set `user_version` backwards. No production restore procedure was exercised here.

## Refresh and inspect

```sh
.venv/bin/vader --db data/settlement.sqlite3 settlement refresh \
  --ticker KXMLBGAME-26SEP271505LADSF-LAD --budget 60
.venv/bin/vader --db data/settlement.sqlite3 settlement inspect \
  --ticker KXMLBGAME-26SEP271505LADSF-LAD --history --limit 20
.venv/bin/vader --db data/settlement.sqlite3 settlement replay
```

Repeat `--ticker` to select contracts explicitly. Without it, discovery requests
`KXMLBGAME` with `--status settled` (default), `closed`, or `all`. Defaults are 20
targets, two pages and 120 seconds; hard maxima are 50 targets, five pages and 300
seconds, also constrained by configured market/page limits. Discovery that reaches
a limit with unscanned results reports `partial` and `selection_truncated=true`.
Use explicit tickers when verifying a small selection rather than completeness of
the entire series. An empty discovery result is `inconclusive`, never a fixture pass.

The existing reader enforces one request in flight, at most two requests/second,
three attempts per request, a 45-second request budget, TLS verification, endpoint
allowlisting, response-size caps and bounded retries. 401/403 abort the refresh;
other target errors retain raw evidence and report partial collection. The overall
budget controls network admission and retry deadlines; local commits/cleanup may
finish after that deadline. Processes using different databases have independent
rate limiters: integration must serialize provider work at the deployment level.

JSON status `complete` means the selected evidence was collected, **not** that every
mapping resolved or every contract settled. Check `mapping_states`, interpretation
flags and provider lifecycle. Exit codes: 0 complete, 3 inconclusive, 1 failure or
partial, 130 operator interruption. Quarantine is a persisted identity decision,
not a transport error. Inspection is read-only and uses one SQLite read snapshot.

An interrupted/partial run can resume with identical code/configuration/options:

```sh
.venv/bin/vader --db data/settlement.sqlite3 settlement refresh \
  --ticker KXMLBGAME-26SEP271505LADSF-LAD --budget 60 --resume RUN_ID
```

Completed requests reuse their archived retrieval; unfinished requests retry with
new retrieval identities. Changed code/options require a new run. An invalid
discovery cursor requires a new run; old retrievals remain archived. Each resume
gets a fresh bounded invocation budget. Complete/inconclusive runs cannot resume.

## Provider semantics, verified 2026-09-28

The [Kalshi market schema](https://docs.kalshi.com/api-reference/market/get-market)
defines `settlement_value_dollars` as the YES/long payout, and `settlement_ts` as
the settlement timestamp. The `settled` query returns REST `finalized` markets.
`is_provisional` concerns removal of inactive markets after determination; it does
not mean a payout is provisional. Missing fields stay absent in the stored source
projection, and interpreted values remain null when unavailable.

The [market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle)
distinguishes `determined`, `disputed` and `amended` from completed `finalized`
settlement. The journal records `provider_amended` separately from an observed
correction. Its revision numbers describe ingestion history, not provider revision
IDs. Provider timestamps are preserved verbatim; retrieval times live in `fetches`.
Latest means most recently observed, not necessarily greatest provider timestamp.

Prices/payouts are decimal strings; arithmetic uses `Decimal`. For reviewed $1
contracts, NO payout is explicitly derived as `1 - settlement_value_dollars`.
Only a consistent finalized YES/1 or NO/0 with valid settlement timestamp gets a
binary outcome. `scalar`, fractional payouts and contradictory result/payout pairs
remain exceptional and have no binary label. Unknown or malformed values are
flagged; no payout is inferred from `result` alone.

The reviewed [baseball terms](https://assets.kalshi.com/contract_terms/BASEBALLGAMEWIN.pdf)
cover full games including extra innings, postponements, cancellations, ties and
exchange-determined fair prices. Official MLB results are evidence, not an
implementation of Kalshi's discretion. Later MLB corrections do not rewrite a
contract settlement. Changed contract-document bytes quarantine mappings until
reviewed, while the independent Kalshi record remains available.

Reviewed source hashes (SHA-256; downloaded payloads are ignored local artifacts):

| Source | Hash |
|---|---|
| Market schema `.md` | `0a7e648e91ccabcf8b6014bb0dde471b97ab44e9ead5c3fafb24217ca19a0b6a` |
| Lifecycle `.md` | `5f3f7c72eda96148fd543e516f506110998bc4a7d58877ec36bcb0454f2d2510` |
| Baseball terms PDF | `46b02443153f4692acb3bac3d3aedabe93e837b08c80323013c8dce117ebb6e7` |

## Mapping and evidence

Mapping uses the reviewed aliases in `scope.py`, the exact full-game rule,
canonical away/home teams, original start/date and explicit doubleheader number
when supplied. The additional doubleheader rule wording was observed on live
`KXMLBGAME-26SEP251305CHCBOSG1`. Unique exact original starts can distinguish games;
rule/ticker game-number disagreement quarantines. There is no fuzzy matching.

A seven-day official [MLB schedule](https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=2026-09-25&endDate=2026-09-28)
window surrounds the original date. A timestamp-level `rescheduledFrom` link or
previously evidenced stable game ID can support a changed schedule. A date-only
link, changed start without evidence, ambiguous candidates, reversed home/away,
unknown scope/rules/aliases or TBD start cannot prove identity and is quarantined.
Postponed/cancelled/suspended status is retained independently of the mapping and
never manufactures a payout. An already matched identity may remain matched while
its game status changes.

MLB may repeat game IDs in a multi-date response. Identical normalized entries are
one observation with the full response retained. Conflicting entries are retained
as an `ambiguous` official-result version with all variants; an affected mapping
is quarantined. Unrelated games continue. This includes some provider-linked
postponed/final pairs: automatic reconciliation of those variants is deferred.
MLB schedule data has no guaranteed finalization timestamp, so none is invented.

The schema separates version content from observations:

- `settlement_versions.kind` partitions `mapping`, `mlb`, and `kalshi` records.
  A changed semantic projection appends a revision linked to its predecessor.
  A→B→A remains three revisions. Unchanged content reuses the current version.
- `settlement_observations` links every actual retrieval to its version. The unique
  `(kind, entity_key, fetch_id, parser_version)` key prevents replay duplication.
- `settlement_targets` links each refresh to its terms, market, event, schedule
  retrievals and final mapping version. Repeated identical bodies get new fetch
  IDs, while only byte storage deduplicates. All source bytes remain in `blobs`.

Response bytes, normalized records and target progress commit together. Parsing
errors retain their raw retrieval while rolling back normalization. Process
interruption before commit leaves a recoverable unfinished request; after commit,
resume reuses the successful retrieval. Replay is ordered by fetch sequence in
bounded batches and works through either `vader replay` or `settlement replay`.
It never re-fetches the network. Replaying a previously malformed retrieval can
continue to report that failure. It does not erase earlier parse-error evidence.

Inspection returns Kalshi revision history, the latest mapping and MLB result,
first retrieval time, source timestamps, observation count, and evidence IDs.
`--history` applies to Kalshi revisions; latest mapping/MLB fields are explicitly
labelled and are **not** historical as-of joins. For all provider revisions:

```sql
SELECT kind, entity_key, revision, previous_id, change_kind, first_fetch_id, data_json
FROM settlement_versions WHERE entity_key IN ('823164','KXMLBGAME-26SEP271505LADSF-LAD')
ORDER BY id;

SELECT o.kind,o.entity_key,o.version_id,f.id,f.seq,f.retrieved_at,f.body_sha256,
       r.url,r.params_json
FROM settlement_observations o JOIN fetches f ON f.id=o.fetch_id
JOIN requests r ON r.id=f.request_id ORDER BY f.seq LIMIT 100;
```

## Validation and boundaries

```sh
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q
.venv/bin/uv build
```

All offline tests forbid real HTTP. They cover baseline migration and rollback,
archived-book preservation, ambiguity and schedule changes, payout types, missing
fields, A→B→A corrections, repeated refresh/replay, crash recovery, cursor loops,
limits, authorization failures, collector health and ordered reconstruction.
See [the handoff](handoffs/settlement.md) for actual results and separate live evidence.

No scheduler, live database, trading system, forecasts or evaluation code is
changed. No new dependency is required; the baseline `uv.lock` remains unchanged.
Automatic reconciliation outside the bounded schedule window, manual mapping
overrides, historical MLB correction recovery and production restore testing are
not implemented. Unknown cases remain inspectable quarantines rather than guesses.
