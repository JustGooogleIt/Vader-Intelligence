# F3.3b evidence half: record contract 2, witness and availability projection

Branch `feat/f33-record-contract-2`, stacked on `feat/f33a-discovery-boundary`, which is
stacked on PR #8 (`deea149`) on `main` `ebb58df`. Implemented 2026-10-12 on this Mac in
the same isolated worktree and `uv sync --locked` environment (Python 3.11.6, uv
0.12.19). No production archive, service or configuration was touched; migration 004 ran
only on disposable databases; nothing was merged or deployed.

Governing documents: [specification](../specs/forecast-evaluation-v1.md) §2, §5, §6 and
the [F3.3 implementation plan](../specs/f33-implementation-plan.md) §4, §9.1–§9.3, §9.6,
§9.7. The requirement trace is the [implementation checklist](../specs/f33-implementation-checklist.md),
part B. This branch depends on the discovery boundary in part A.

## Record contracts (plan §9.6)

A stored research record declares its contract in its own canonical payload:

- **No `contract_version` field → contract 1**, the rules on `main` at `ebb58df`,
  frozen and kept as the contract-1 validator. Every row the shipped tests and any
  operator's journal-API calls have written so far is contract 1.
- **`contract_version: 2` → contract 2**, defined for `forecast_receipts` and
  `forecast_decisions` only. Anything else (an explicit `1`, any other integer, a
  non-integer, or `2` on another table) is unknown and rejected on insert and in
  integrity.

Contract 2 is admissible only on archive schema 4, reached by the explicit migration
`vader forecast migrate --record-contract 2`. Migration 004 contains no executable SQL:
it appends the `schema_migrations` ledger row and sets `user_version` 4 in one
transaction. Its purpose is detection. Every schema-version guard in `src/` and `ops/`
now accepts 4, and a binary that predates contract 2 refuses a schema-4 archive at open,
rather than opening it, writing contract-1 rows beside contract-2 rows, and failing
later as apparent corruption. A genuine proof of that refusal is in
`test_record_contract.py::test_binary_at_main_refuses_a_schema_four_archive`, which
extracts `src/` at `ebb58df` with `git archive` and runs its real CLI.

Contract 1 is never converted, rewritten or upgraded. On schema 4 it is closed to
**new** receipts and decisions, but an exact retry of an existing contract-1 envelope
still returns its original id, and contract-1 rows stay valid, inspectable and counted.
`journal.integrity` and `ops/manage.py inspect` report per-table, per-contract counts
under `research.record_contracts`, plus the latest immutable discovery fact.

What changes between the contracts, and nothing else:

| Record | Contract 1 | Contract 2 |
|---|---|---|
| Fetch receipt digest | raw body hash | the fetch's typed row reference digest (whole completed attempt, body or not) |
| Receipt key | subject, digest, namespace, mode | the same plus the contract version |
| Receipt config/run link | caller assertion | `config_hash` equals the witness run's configuration digest |
| Receipt mode | any | forward_shadow or synthetic only (M4) |
| Decision → discovery pass | equality of `config_hash` | exact pin `{id, digest, collector_config_hash}` |
| Decision modes | equal to parents | M9/M10 (reconstruction may cite live evidence) |
| Decision parents | any valid | may not cite a contract-1 receipt or supersede a contract-1 decision |

## Witness (`forecast/witness.py`, plan §4.2–§4.4)

A finite library function, with **no caller in this increment**: the collector never
imports it, and no command or scheduler invokes it. Until the F3.4 `forecast tick`
supplies a caller, no operational receipt exists and the projection reports every live
input as `availability_unproven`. Tests call it with an injected clock, which forces
synthetic lineage.

One invocation: hold the writer lock; require schema 4; start a `forecast-witness` run;
sample a session baseline (UTC and monotonic); then repeatedly, on a **separate
read-only connection**, select subjects and compute each subject's typed reference
(re-hashing raw bodies), close the read, sample UTC and monotonic **once** as the
`observed_at` for the whole batch, run the clock checks, and append one receipt per
subject through the journal. The read and the observation always precede the insert; a
crash between them loses the proof, not the evidence.

Subjects: discovery passes newest-first, at most `max_passes` (100), with no contract-2
receipt in this namespace and mode; and completed fetches (`ok`, `parse_error`, `error`
with a `retrieved_at`) among the newest `scan_rows` (5000) by `seq`, at most
`max_subjects` (500) newest-first, inserted ascending by `seq`. Pending and interrupted
attempts are counted (`pending_seen`, `interrupted_seen`) and never witnessed. Anything
unwitnessed outside the scan window is reported as `outside_window` and stays unproven.
Hitting a cap or the budget finishes the run `partial` with the remaining count.

Clock status per observation: `untrusted` when UTC-vs-monotonic session divergence
exceeds 2 s, when `observed_at` is below the high-water mark (the newest trusted
`observed_at` in this namespace and mode among the last 10 000 receipts), or when
`observed_at` precedes the subject's `retrieved_at`/`created_at`. An untrusted receipt is
written and permanent; the subject stays unproven. A restart resets only the monotonic
baseline; it does not reinterpret any written receipt, reset the regression mark, or show
that the wall clock recovered. The HTTP `Date` header is not consulted by any check.

What local clocks cannot establish is stated in the plan §4.4 and is unchanged here: a
forward step between sessions, a small backward step, or a shared constant offset are
undetectable. A receipt proves ordering on the local clock's timeline, not against true
UTC. There is no trust override, no command and no configuration value that marks a clock
trusted or rewrites a receipt.

## Availability projection (`forecast/evidence.py`, plan §4.6)

Read-only, no clock, no writes. `fetch_availability(db, fetch_id, namespace, mode)`
returns the fetch's `retrieved_at` with the `observed_at` of its first trusted contract-2
receipt, or no observation time. `discovery_as_of(db, cutoff, namespace, mode)` returns
the applicable discovery record for a cutoff C using only records whose trusted
contract-2 receipt was observed at or before C:

1. Candidates are witnessed discovery passes and **orphans**: a witnessed
   discovery-stage fetch (`status`, `series`, `document`, `markets`, `event`) of a
   two-phase run whose `seq` is beyond every witnessed pass of that run. An orphan means
   a pass started and committed no summary.
2. The applicable record is the one furthest along in ingestion order (pass id, orphans
   by `seq`). Sequence numbers only order already-witnessed records.
3. An orphan yields `universe_complete=False`, reason `discovery_summary_missing`. An
   incomplete pass yields its own reasons. Neither falls back to an older complete pass.
4. A complete pass is usable only if the reader's own verification passes: known
   manifest version, consistent `phase_reached`/`candidates_known`/`eligibility_complete`/`pages_complete`,
   no reasons, mode not reconstruction, written by a two-phase collection run, and
   `config_hash` equal to its run's configuration digest. Otherwise it is
   `universe_incomplete`.
5. `DiscoveryEvidence.times` feeds the merged F3.1 `policy.freshness`, which applies the
   300-second limit to `completed_at` and the cutoff to `observed_at`. A contract-1
   receipt contributes nothing whatever time it claims.

## Cohorting (specification §5, plan §9.2)

`journal.check_cohort`, run inside `append_evaluation` and `integrity`, enforces that one
evaluation holds exactly one collector configuration hash (read from each contract-2
decision's pin) and does not mix contract-1 and contract-2 decisions. A changed collector
configuration is not rejected; it forms a separate cohort reported separately. Results
are never pooled across collector hashes. The research protocol hash stays distinct from
the collector hash; changing it alone makes a different scoring-unit key, not a conflict,
and leaves the pinned collector hash untouched.

## The working vertical slice (assignment §4)

`tests/forecast/test_vertical_slice.py` drives the whole path on a schema-4 archive: a
two-phase `collect` commits the summary and books, the next tick's `witness` observes the
committed pass and fetches at a later real time, `discovery_as_of` selects the applicable
pass, and `policy.cutoff_eligibility` with the F3.1 baselines produces a decision for a
cutoff after the observation and an abstention before it. Book outcome never changes
discovery eligibility; an incomplete pass abstains and collects no book; replay preserves
every receipt and reports per-contract counts.

Decision, publication and evaluation **generation** from live evidence are not in this
increment. This branch delivers the storage contracts, the witness that produces
receipts, the projection that feeds the pure policy, and the cohort rules the F3.5 plan
will build on. The selection and publication services (F3.3b remainder), the research
configuration parser and tick (F3.4), and evaluation materialization (F3.5) remain their
own plans.

## Verification actually performed (this Mac, worktree, disposable databases)

Final-head results are in the morning handoff. Commands:

```sh
.venv/bin/ruff check src tests ops
.venv/bin/ruff format --check src tests ops
.venv/bin/python -m pytest -q tests/forecast_storage tests/forecast tests/test_integration.py
.venv/bin/python -m pytest -q
.venv/bin/python -m unittest discover -s tests/ops -q
uv build
uv venv .venv-wheel-f33
uv pip install --python .venv-wheel-f33/bin/python dist/vader_intelligence-0.1.0-py3-none-any.whl
.venv/bin/python tests/integration/wheel_smoke.py --python "$PWD/.venv-wheel-f33/bin/python"
git diff --check
```

The installed-wheel smoke passed 19 checks including `forecast migrate`, the schema-4
backup/restore/replay path and the schema-5 unknown-version rejection. `uv build` used the
pinned uv 0.12.19 from the stable checkout's environment.

## Limits and what is not claimed

- No live witness run exists; every receipt in the tests is synthetic because the clock
  is injected. The `forward_shadow` paths are exercised with synthetic fixtures and are
  not live evidence.
- The old-binary refusal test needs `git archive ebb58df` to succeed; it skips (does not
  fail) if that revision is unavailable in the checkout. It ran here.
- Migration 004 is reversible only as a separate copy: a schema-4 archive cannot be read
  by any binary without this branch, and rollback to `main` after migration is not
  code-only (plan §5.5). Migrate only on an explicit command, after a backup to a new
  path.
- `journal.integrity` re-validates every typed reference of every pass and receipt; its
  time grows with history (plan risk R3). The F3.3a handoff records 0.30 s at 720 passes;
  receipts add to that and should be remeasured before long-running use.
