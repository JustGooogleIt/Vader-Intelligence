# F3.3b-remainder and F3.4: forecast tick and selection service

Branch `feat/f34-tick-selection`, stacked on PR #10 (`feat/f33-record-contract-2`, tip
`841af2a`), on PR #9 (`8f9f2c0`), on PR #8 (`deea149`), on `main` `ebb58df`. Implemented
2026-10-12 on this Mac in an isolated worktree with its own `uv sync --locked`
environment (Python 3.11.6, uv 0.12.19). No production archive, service or configuration
was touched; nothing was merged or deployed. Every database used was disposable and every
record is synthetic lineage (injected transport and clock).

Governing documents: [specification](../specs/forecast-evaluation-v1.md) §2, §3, §4, §6,
§7. Requirement trace: [implementation checklist](../specs/f34-implementation-checklist.md).
PRs #9 and #10 are provisional dependencies under review; this branch extends the witness
additively (new subject kinds) and otherwise adds new modules.

## What this delivers

A complete path from eligible collected evidence to a persisted forecast decision or an
explicit abstention, invoked by a finite `forecast tick`:

- **Research protocol** (`forecast/research.py`): the versioned protocol parsed from a
  separate `[research]` TOML, with the v1 defaults (protocol `mlb-t60-v1`, horizon 3600,
  freshness 300, grace 150, epsilon `0.000001`, max 100 games / 200 contracts) and a
  canonical `config_hash` distinct from any collector hash. Out-of-contract settings are
  rejected.
- **Binding preparation** (`forecast/service.py:prepare_binding`): the single-revision
  identity binding for a game from a discovery pass's eligibility evidence — gamePk, both
  team IDs, original start, game number, the YES team chosen by `selection.select_contract`
  (lexically smallest event, lower MLB team ID), the reviewed terms hash, and the
  event/schedule/terms evidence tuple. The binding cutoff is the forecast horizon
  `C = start - 3600`, distinct from the collector's T-120 book guard. Idempotent per
  cohort.
- **Input selection** (`forecast/selection.py:select_inputs`): the midpoint book is the
  latest completed book retrieval received **and** witnessed by C, 0..300 s fresh, with
  complete rejection reasons (`book_missing`, `book_availability_unproven`,
  `book_availability_after_cutoff`, `book_truncated`, `book_fetch_failed`,
  `book_parse_error`, `book_stale`, `book_schedule_mismatch`). No backward search, no
  post-C snapshot. Arithmetic-level reasons (side missing, crossed, flagged) come from
  `baselines.midpoint`. The module stays import-pure (evidence imported lazily).
  Correction (2026-10-20, decision D-016): as implemented, the default rule, now named
  `selection-v1`, anchors on the latest retrieval received by C and abstains when that
  retrieval was witnessed after C. The rule described above is the opt-in
  `selection-v2` (#36).
- **Decision generation** (`forecast/service.py:decide_opportunity`): composes
  `policy.cutoff_eligibility` and the F3.1 baselines into a **contract-2** decision pinned
  to its discovery pass. Constant emits `0.5` when the common checks pass; the midpoint
  abstains with reasons otherwise; a stored probability is never erased by later fields.
- **Publication** (`forecast/publication.py`): `policy.classify_publication` decides the
  verdict; the separate, immutable `forecast_publications` record carries it. The allowed
  record's timeliness is confirmed only by a later witnessed receipt.
- **Tick** (`forecast/tick.py:tick`): the finite sequence — witness prior fetches and
  passes; prepare and witness bindings; compute due decisions from pre-C witnessed
  evidence; attempt publication separately; record missed; exit. `inspect` gives a
  read-only, keyset-paginated view of what each decision considered and why.
- **Witness extension** (`forecast/witness.py`): now also witnesses `binding` and
  `publication` subjects; `evidence.binding_availability` reads a binding's witnessed
  availability.
- **CLI** (`cli.py`): `vader forecast tick --research-config FILE --dataset D --budget N`
  and `vader forecast inspect --limit N --before ID`. No daemon, no scheduler.

## Semantics

**Cutoff and availability.** Selection reads only evidence retrieved at or before C and
witnessed (a trusted contract-2 receipt observed at or before C). The read-side
projection admits no receipt observed after C, so a receipt made now cannot authorize an
input for an earlier C, and a later arrival or observation cannot leak into a historical
decision. The 300-second freshness limit is applied by the pure `policy.freshness`
exactly as in F3.1. The HTTP `Date` header is read by nothing in this path; it confers no
clock trust or eligibility (specification §2 decision Q2).

**Distinct clocks.** Decision time (the binding's C), publication time (the attempt and
the witnessed release), observation time (receipt `observed_at`) and persistence time
(`created_at`) are stored separately and never substituted. The decision freezes the
cutoff facts; publication and its witness are separate records.

**Idempotency and transactions.** Each record is written in its own `BEGIN IMMEDIATE`
journal transaction under the existing `storage.writer_lock`; no network call is inside a
transaction. Keys:

| Record | Idempotency key | Repeat behaviour |
|---|---|---|
| Binding | `(candidate_key=gamePk, policy_version, config_hash, mode, revision)` | `prepare_binding` returns the existing cohort binding unchanged |
| Decision | `(dataset, mode, protocol, config_hash, gamePk/unresolved_key, horizon)` | the tick skips a game whose decision exists; a repeat tick adds nothing |
| Publication | `decision_id` (one per decision) | the schema enforces one; only the creating tick attempts it |

Because the decision key excludes time, ticker, schedule revision and snapshot, repeated
or concurrent ticks cannot create a second or retroactive forecast. Concurrency is
serialized by the writer lock: a second concurrent tick either reports `writer_busy` or
runs after and finds the decision present.

**Restart and crash.** A crash before a record commits leaves nothing half-written (the
transaction rolls back); the next tick re-derives. A crash after the decision commits but
before its publication leaves the decision standing with no publication, which reads as
`not_attempted` (specification §6); the decision is **not** retroactively published by a
later tick, because the decision already exists and is skipped. This is the minimal,
safe behaviour; a future increment could let a later tick publish a still-within-window
decision that lacks a publication, but that is not implemented and is documented here so a
reviewer can decide whether it is wanted.

**Replay.** `replay` is unchanged: it re-normalizes raw responses, preserves every
research row and reports `preserved_not_reconstructed` with per-contract counts. It
fetches nothing present-day. `service.redecide` is a distinct, read-only diagnostic that
recomputes a decision from the same persisted inputs (same binding C, same projection,
same book) and is used to show replay reproduces the stored result; it writes no record.

**Status and exit codes** (specification §7): the tick returns `complete` (decisions
made), `inconclusive` (no due decisions — e.g. no eligible markets, mapped by the CLI to
exit 3), `partial` (an error during the tick, exit 1), or raises on interrupt (exit 130).
A reason-coded abstention is a successfully recorded decision, never a fabricated error.

## Measured (synthetic, this Mac)

One game, schema-4 archive, injected clock: warm tick (witness + prepare + witness
binding) 93 ms; due tick (witness + decide + publish + witness) 53 ms. Well inside the
15 s / 30 s budgets. This is the storage-and-logic cost, not provider latency, and not a
coverage claim.

## Deferred, with handoff to later increments

- **Schedule-revision state machine** (specification §2 "Schedule version selection",
  `policy.decide_schedule`): moving C when a supported start revision is observed before
  both the old and new C, multi-revision binding chains, `schedule_revision_missed_cutoff`
  and `first_seen_after_cutoff`. Not implemented. This block prepares one binding revision
  per game; the decision key already makes the first reached C sticky, so a later tick
  never creates a second forecast for the same game/horizon/cohort. A game whose start
  changes after its first binding keeps the original binding and C. Implementing the state
  machine is the natural next F3.4 increment; it needs `decide_schedule` as a pure
  function plus binding renewal in the tick.
- **`forecast reconstruct`** (specification §7): the archive-only historical campaign with
  an explicit dataset namespace and source ceiling, mode `historical_reconstruction`. Not
  implemented; the forward tick is the smallest complete path. The record contracts it
  needs (contract-2 decisions, the pin, M9/M10 reconstruction-of-live rules) are in place
  from #10, so reconstruct is additive.
- **F3.5 evaluation materialization** (`evaluate`, `evaluation inspect`,
  `evaluation.outcomes.join`): explicitly out of scope. **Upstream contracts it needs are
  complete**: contract-2 decisions with the discovery pin and collector-hash provenance,
  publications with verdicts, witnessed receipts, and `journal.check_cohort` already
  enforcing one collector hash per evaluation and no contract-1/contract-2 mixing. F3.5
  must: freeze a population/decision manifest, join outcomes via the settlement journal at
  an `outcomes_as_of` ceiling, score with the pure `evaluation.scoring`, and persist
  `evaluation_runs`/`evaluation_items` (both already defined and validated in the journal).
  The forward-shadow view scores only `published_timely` decisions; the
  cutoff-reconstruction view scores the frozen cutoff population. No new upstream storage
  change is required.

## Verification (this Mac, worktree, disposable databases)

Final-head commands and results are in the morning handoff. In summary: ruff check and
format clean; `pytest -q` 729 passed with 2 pre-existing `tests/worker_observer` failures
(absent `tmux_jev`, untouched by this branch); operations 31 run / 1 skipped; `git diff
--check` clean; `uv build` plus an installed-wheel smoke (22 checks) exercising
`forecast migrate --record-contract 2`, `forecast tick` and `forecast inspect` from
outside the checkout.
