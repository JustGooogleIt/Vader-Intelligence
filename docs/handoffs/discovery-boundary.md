# F3.3a discovery boundary: two-phase collection on schema 3

Branch `feat/f33a-discovery-boundary`, stacked on PR #8 (`deea149`, documentation only),
which is itself on `main` `ebb58df`. Implemented 2026-10-12 on this Mac in an isolated
worktree with its own `uv sync --locked` environment (Python 3.11.6, uv 0.12.19). No
production checkout, archive, service or configuration was touched; no migration was run
on anything but disposable databases; nothing was merged or deployed.

Governing documents: [specification](../specs/forecast-evaluation-v1.md) §3, §6, §8 and
the [F3.3 implementation plan](../specs/f33-implementation-plan.md) §3, §5, §8. The
requirement-to-code-to-test trace is in the
[implementation checklist](../specs/f33-implementation-checklist.md), part A. Plan
departures are in plan §13.

## What a schema-3 collection now does

1. **Phase A, discovery.** Exchange status, series identity, both contract documents,
   the bounded market scan (and the `refresh:` rescan on resume), the official schedule,
   one event fetch per candidate event, and one `scope.eligible` decision per candidate,
   each recorded as today. No book request is made. Schedule evidence is refreshed when
   its age nears `schedule_max_age`, as today.
2. **Summary.** One `discovery_passes` row is built from the committed rows
   (`forecast/discovery.py:build_summary`) and appended through the forecast journal in
   one `BEGIN IMMEDIATE` transaction, with every typed reference re-validated inside it.
   This is the last step before any book request. A pass that failed in Phase A commits
   an **incomplete** summary with its reasons in the fixed order of plan §3.1.
3. **Phase B, books.** Only when the committed summary is complete and the command is
   not `discover`. Same contract order as the legacy loop. If the schedule behind a
   contract's decision is older than `schedule_max_age − 5` seconds, a fresh schedule is
   fetched, `scope.eligible` is re-run against the Phase A event response, and a **new**
   eligibility row is appended after the summary bounds. The unchanged `before_attempt`
   guard (T−120 cutoff and schedule freshness) and budget apply to every book.
4. **Finish.** `runs.summary_json` keeps every existing field with its existing meaning
   and adds `discovery_pass_id`, `discovery_digest` and `discovery_reasons`.
   `discovery_complete` is copied from the committed summary. Book errors still make the
   run `partial`; a 401/403 still fails it.

Schemas 1 and 2 run the reviewed interleaved loop, moved verbatim to
`Collector._run_legacy`. Their tests are unmodified and pass.

## The persistence boundary

"Incomplete discovery retains no books" is enforced in `storage.py`, not only by the
collector's control flow. `Store.request` for stage `book` calls
`Store._require_complete_discovery` inside the request transaction. For a run whose
provenance declares `collection_protocol: "two-phase-v1"` it requires that the run's
**latest** committed discovery pass belongs to the requesting invocation (the session
prefix of the existing `session:ticker` request key) and is complete. Otherwise it raises
`DiscoveryGateError` before any request row, fetch row or network request exists. Legacy,
fixture and research runs declare no protocol and are unaffected. The test
`test_bypassing_caller_cannot_retain_books_after_incomplete_discovery` drives a
fabricated eligible decision into Phase B after an incomplete pass and shows the Store
refusing it with zero orderbook requests.

## Recording failures honestly

| Failure point | `phase_reached` | `reasons` | Candidates | Run status |
|---|---|---|---|---|
| Status, series or document unavailable | `none`, `status`, `series` | stage reason | none, not known | `failed` |
| Series identity wrong | `status` | `series_identity_mismatch` | none | `failed` |
| Pagination bound, repeated cursor, page unavailable | `documents` | the bound's reason or `pages_incomplete` | observed so far, not known | `failed` |
| Schedule unavailable | `pages` | `schedule_unavailable` | known, no decisions | `failed` |
| Event unavailable, identity mismatch, not two markets | `schedule` (or `events`/`eligibility` if another event was decided) | that event's reason | known, some undecided | `partial` |
| Event omits a discovered contract | `events` | `eligibility_incomplete` | known, undecided | `partial` |
| 401/403 anywhere in Phase A | last finished stage | `access_denied` | as reached | `failed` |
| Operator interrupt in Phase A | last finished stage | `interrupted` | as reached | `interrupted` (best-effort summary) |
| Summary insert fails | no row | run `errors` carries `discovery_summary_not_committed` | raw rows kept | `failed`, no books |
| Zero open markets, terminal page | `done` | none; `complete=true`, counts 0 | known, empty | `inconclusive` |
| Every candidate ineligible | `done` | none; `complete=true`, counts 0 | known | `inconclusive` |

A completed failed attempt (HTTP error, timeout, truncation, interrupt recorded by the
reader) is a typed reference in the summary. A `pending` or `interrupted` attempt is
listed under `manifest.incomplete_attempts` and is never a reference. A process killed
before the summary commits leaves no summary; its resume starts a new session, a fresh
discovery and a new pass row with `phase_sequence` 1 (2 if the earlier session's summary
had committed). No existing pass row is ever updated.

## Operator visibility

- `vader collect --once` / `discover` / `live-check` output and the stored
  `runs.summary_json` carry `discovery_complete`, `discovery_pass_id`,
  `discovery_digest` and `discovery_reasons`. An incomplete pass always has at least one
  line in `errors`.
- `vader health` on schema ≥3 adds `latest_discovery` with `id`, `run_id`, `session_id`,
  `complete`, `reasons`, `counts`, `completed_at`, `phase_reached` and `mode`.
  Complete-and-empty is `complete: true` with zero counts; incomplete carries reasons.
- `ops/manage.py` classification is unchanged and already distinguishes
  `discovery_no_eligible_games` (exit 3, complete, empty) from `failed_or_partial`.
- Replay, inspection, backup and restore re-validate every pass through the existing
  schema-3 integrity pass.

## Verification actually performed (this Mac, worktree, disposable databases)

Final-head results are recorded in the morning handoff; the commands are:

```sh
.venv/bin/ruff check src tests ops
.venv/bin/ruff format --check src tests ops
.venv/bin/python -m pytest -q tests/test_discovery_boundary.py
.venv/bin/python -m pytest -q
.venv/bin/python -m unittest discover -s tests/ops -q
git diff --check
```

At commit `db64ddd` (code and tests): `tests/test_discovery_boundary.py` **38 passed**;
full suite **595 passed, 2 failed** (the two pre-existing `tests/worker_observer`
failures need the `tmux_jev` package, which is not in the locked environment, and are
unrelated to this work); operations **31 run, 1 skipped**; lint, format and diff checks
passed. The SIGKILL case runs a real child process against the worktree's tests.

## Synthetic measurement at 720 passes (plan §8.4)

Script: 720 consecutive two-phase `collect` runs of the shipped fixture (2 contracts,
1 game, 7 discovery fetches and 2 books per pass) against one disposable schema-3
database on this Mac, with the MockTransport and an injected clock. This measures the
storage path, not provider latency, and is not live evidence.

| Quantity | Measured |
|---|---|
| Summary payload size (canonical JSON) | min 6,247 B, median 6,282 B, max 6,286 B |
| Summary insert through `Store.append_forecast` (contract validation, reference re-hashing, BEGIN IMMEDIATE, FULL sync) | median 4.3 ms, p95 5.5 ms, max 15.7 ms |
| Whole two-phase run with 9 mocked retrievals | median 119 ms, max 3.2 s (one outlier) |
| `journal.integrity` over 720 passes (re-validates every typed reference, re-hashing raw bodies) | 0.30 s |
| `Store.verify_integrity` (blob hashes, quick_check, FK check, journal integrity) | 0.20 s |
| Database size after 720 passes | 20.3 MB (dominated by the raw fetch and blob history of 6,480 retrievals, not by the passes) |

At ~6.3 KB per pass, 720 passes a day add about 4.5 MB/day of summary payload before
indexes. A live pass with up to `max_markets` candidates will carry a larger candidate and
eligibility manifest; the 1 MiB cap bounds it. Integrity time grows with pass count and
should be remeasured before long-running use (plan risk R3). Script and raw output are
outside the repository (session scratchpad); the figures are from one run on this Mac.

## Limits and what is not claimed

- No live collection was run on this branch; lineage of every stored pass in the tests is
  `synthetic`. A live run with the default `Reader` records `forward_shadow`.
- The 1 MiB manifest cap (`manifest_too_large`) is implemented but not exercised: the
  collector's own `max_markets` cap keeps fixture manifests far below it.
- No receipt, witness or projection exists in this increment; a pass is evidence of
  completion, not of availability (specification §2). Those follow in
  `feat/f33-record-contract-2`.
- No production archive is at schema 3 as far as this work knows; merging this branch
  deploys nothing and changes nothing on schemas 1 and 2.
- Rollback to `main` is code-only while the archive is at schema 3: every row written here
  is a contract-1 discovery pass that the current `main` validates (plan §5.5).
