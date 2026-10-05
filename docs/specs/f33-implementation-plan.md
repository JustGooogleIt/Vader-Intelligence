# F3.3 implementation plan: independent discovery completion and evidence-availability witnessing

Status: **plan only, not implemented**. Written 2026-10-04 by the third engineering
session (Claude) in its own checkout. This document changes no application code,
schema, service, archive or other pull request.

Revision 2 (2026-10-04) turns the three interface gaps found in revision 1 into
concrete contracts (§9), adds the mode relationship table, states the discovery
failure policy with its governing wording, narrows the witness and clock claims, and
checks that an incomplete discovery can be recorded honestly (§3.5). `main` and the
PR #7 head were rechecked and are unchanged from the SHAs below.

Governing documents: [forecast/evaluation specification](forecast-evaluation-v1.md)
(§2, §3, §6, §7, §8), [F3.1 handoff](../handoffs/forecast-policies.md), and the F3.2
storage handoff `docs/handoffs/forecast-storage.md` as it exists on the PR #7 head.
Where this plan and the specification disagree, the specification wins and the
disagreement is a defect in this plan.

## 0. Pinned state and what was verified

| Item | Value | How established |
|---|---|---|
| `main` | `804873b841d45d9e4e3478dca9feba86fa36a6ea` | Observed on GitHub and in a fresh clone, 2026-10-04 |
| PR #7 head (F3.2 storage) | `acc4c2458626f757152904868d42263447133c35` | Observed: open, draft, unmerged, mergeable, no GitHub review recorded |
| PR #6 head (worker observer) | `a3625086b18de838a3130fb559336a1f00deed71` | Observed: open, draft. Not read, not a dependency of this plan |
| Merged before `main` | PR #1, #3, #4, #5 (F3.1 pure policies) | Observed on GitHub |
| Engineering skills | Cool Coder 1.2.0 at `e46e79805be0ee5877fa9bc993492064bbb40aa5` | Isolated read-only clone; shared installations untouched |

Skills consulted from that isolated clone: `planner`, `detail-planning` (document
structure and failure-mode analysis), `data-systems-design` (hazard catalog),
`code-review` (scope and hazard checks applied to this document).

Not verified, and not claimed: production collector health, the production archive,
service configuration, any Mac-native test result for PR #7, and the outcome of
Prime's and Secondary's reviews. Handoff statements about those are reports, not
observations made here.

**Unmerged interface dependency.** Everything in this plan that names
`forecast/journal.py`, `forecast/contracts.py`, `forecast/references.py`,
`forecast/schema.py`, migration `003_forecast_evaluation.sql`, or the schema-3 `Store`
guards refers to code that exists **only on PR #7 at `acc4c24`**. None of it is on
`main`, and none of it is copied into this branch. Each such dependency is marked
**[PR #7]**. If PR #7 changes before merge, §9 must be rechecked against the merged
commit before implementation starts.

## 1. Where this work sits in the existing milestones

The specification (§8) already defines the steps. This plan keeps those names.

| Spec step | Spec definition | Covered here |
|---|---|---|
| F3.3a Discovery boundary | Schema-3 collector path; summary commits before any book | **All of it** |
| F3.3b Evidence/selection | Complete candidates, pre-cutoff receipts for summary and inputs, selection, service | **Evidence half only**: the witness and the read-side availability projection |
| F3.3b remainder | Bindings, contract selection, book choice, decisions (`forecast/selection.py` persistence, `forecast/service.py`) | Not covered; needs its own plan |
| F3.4 | `forecast tick` and other CLI, research configuration parser | Not covered. The witness is delivered as a library function that F3.4 calls |

"F3.3" in the assignment therefore means **F3.3a plus the evidence half of F3.3b**.
No milestone is renamed. Throughout, "C" is the T−60 decision cutoff. It is unrelated
to `eligibility.cutoff`, which is the collector's T−120-second book-request guard.

## 2. Goal and non-goals

After this work, on a schema-3 archive:

1. Every collection invocation commits one immutable `discovery_passes` row that states
   whether discovery was complete, with the exact evidence, **before the first book
   request**. Nothing that happens to books afterwards can change it.
2. A separate, later invocation can witness committed fetches and discovery passes and
   append receipts carrying the time it actually observed them.
3. A read-only projection answers, for a cutoff C: which discovery pass applies, was it
   complete, when was it completed, and when was it first observed.

Non-goals: bindings, contract selection, decisions, publication, evaluation, any CLI,
any scheduler or launchd change, any production migration or deployment, any change to
schema-1/2 collection behavior, any new provider request, any change to the 300-second
freshness limit or to tick-before-collection ordering.

## 3. Independent discovery completion (F3.3a)

### 3.1 Evidence required to declare discovery complete

One pass has one scope in v1: open `KXMLBGAME` markets and the official schedule window
the collector already requests. `complete` is true only when every row below holds for
this invocation. Bounds are the existing `Config` values; none is raised.

| # | Evidence | Complete when | Otherwise (reason) |
|---|---|---|---|
| E1 | Exchange status fetch | State `ok` | `status_unavailable` |
| E2 | Series fetch | State `ok`, ticker is `KXMLBGAME`, both document URLs present | `series_unavailable`, `series_identity_mismatch` |
| E3 | Contract terms and product certification documents | Both fetched, state `ok`; raw hashes recorded | `document_unavailable` |
| E4 | Market pagination chain | Ordered pages from the empty cursor to a response with an empty next cursor; every page `ok`; no repeated cursor; ≤ `max_pages` pages; ≤ `max_markets` markets; at most one restart after an expired-cursor HTTP 400, in which case only the restarted chain counts; on resume only the `refresh:<session>` chain counts | `pages_incomplete`, `page_limit_exceeded`, `market_limit_exceeded`, `cursor_repeated` |
| E5 | Official schedule | At least one `ok` schedule fetch for the requested window; every schedule fetch used by a decision is listed | `schedule_unavailable` |
| E6 | Event evidence | For every distinct `event_ticker` among candidates: one `ok` event fetch whose `event_ticker` matches the request and which lists exactly two markets | `event_unavailable`, `event_identity_mismatch`, `event_membership_ambiguous` |
| E7 | Eligibility decisions | Every candidate ticker has an `eligibility` row from this invocation (`run_id` match, id in `(eligibility_after_id, eligibility_through_id]`) that references a schedule fetch from E5 and its event fetch from E6 | `eligibility_incomplete` |
| E8 | Bounds | No deadline exhaustion, access denial (401/403) or interruption during the phase | `deadline_exhausted`, `access_denied`, `interrupted` |
| E9 | Reconciliation | Candidate, event, decision and eligible counts equal the manifest lists they summarize | `internal_error` |

Derived flags: `pages_complete` is E4. `eligibility_complete` is E7. `complete` is all
of E1–E9.

Rules that follow from the specification and existing code:

- An ineligible candidate is **not** incompleteness. Unknown alias, ambiguous game,
  TBD start, postponement and every other `scope.eligible` rejection is an explicit
  decision row, and the pass can still be complete.
- An event response that omits a discovered contract leaves that contract without a
  decision, so the pass is incomplete (current behavior, kept).
- An event that does not list exactly two markets keeps today's fail-closed treatment:
  no decision is recorded for its contracts and the pass is incomplete. Treating it as
  an explicit exclusion instead would be a rule change and is not proposed.
- Zero candidates with a terminal page is a **complete, empty** discovery. It is not
  the same as an unknown scope.
- The research caps of 100 games and 200 contracts (specification §2) are applied by
  the later reader. The collector's own limits are not raised to meet them.
- Completeness never depends on book requests, book results, `runs.status`,
  `runs.finished_at` or `runs.summary_json`.

### 3.2 The persisted record

Stored through `journal.append(db, "discovery_passes", envelope)` **[PR #7]**. Field
values, against the PR #7 version-1 contract:

| Field | Value |
|---|---|
| `run_id`, `session_id` | Collector run and the per-invocation UUID the collector already generates. Unique per invocation; a resume gets a new `session_id` |
| `phase_sequence` | One more than the number of summaries already committed for this run: 1 for the first, 2 for a resume after a committed summary. Cross-run order is the row `id` |
| `completed_at` | UTC sampled when the phase ended (last decision recorded, or the failure) |
| `created_at` | UTC sampled immediately before the insert; never earlier than `completed_at` |
| `code_hash` | `source_hash()`, identical to the run's provenance |
| `config_hash` | The **collector** configuration hash: the digest of this run's recorded `provenance_json.configuration` (§9.2) |
| `mode` | Lineage only (§9.3): `forward_shadow` for a live reader; `synthetic` whenever the transport, clock, sleep or jitter is injected, or the run kind is `fixture`. Never `historical_reconstruction` |
| `complete`, `reasons` | §3.1. Reasons use the fixed order of the table above |
| `counts` | `{"contracts": n, "games": m}`: unique **eligible** contracts and games among the decisions recorded inside the eligibility bounds, the same quantity as the existing run summary. A count of recorded rows, not a claim about the universe (§3.5). Candidate counts live in the manifest |
| `eligibility_after_id`, `eligibility_through_id` | Exclusive and inclusive bounds read at phase start and phase end |
| `references` | One typed `reference(db, "fetches", …)` per distinct **completed** fetch used or attempted in the phase, including completed failures |
| `manifest` | Below |

Manifest (version 1), all lists sorted and bounded by the existing caps:

- `manifest_version` (1), `kind`, `resumed`, `started_at`, `fetch_seq_after`
  (exclusive), `fetch_seq_through` (inclusive): the `fetches.seq` range of this
  invocation's discovery phase.
- `phase_reached`: the last stage that finished, one of `none`, `status`, `series`,
  `documents`, `pages`, `schedule`, `events`, `eligibility`, `done`.
- `candidates_known` (the pagination chain reached its terminal page) and
  `eligibility_started` (at least one decision was attempted).
- `limits`: the configured page, market, schedule-age, pregame-buffer and lookahead values.
- `request`: series ticker, market status filter, schedule window start and end.
- `policy`: parser version, `mapping_version`, and that the predicate is `scope.eligible`
  at `code_hash`.
- `pages`: ordered chain of `{request_id, fetch_id, attempts, has_next}`, `restarted`,
  `terminal`, `page_count`.
- `documents`: `{url, fetch_id, raw_sha256}` for each.
- `schedule_fetch_ids`, `official_game_ids` (every `gamePk` in the fetched window).
- `candidates`: `{ticker, event_ticker, page_fetch_id}`.
- `events`: `{event_ticker, fetch_id or null, market_tickers, error or null}`.
- `eligibility`: `{id, ticker, eligible, reason, game_id, cutoff, schedule_fetch_id,
  market_fetch_id, row_digest}` for every row in bounds.
- `unmatched_events`, `unmatched_games`, `candidate_counts`, `reason_counts`,
  `pages_complete`, `eligibility_complete`.
- `incomplete_attempts`: `{fetch_id, seq, state}` for pending or interrupted attempts,
  which cannot be typed references because they have no completed retrieval.
- `failures`: at most 100 `{stage, key, error}` entries.

Two constraints imposed by PR #7's validator: no manifest object may contain a key
named `table` unless it is an exact typed reference, and the whole record must stay
under 10 MiB. This plan sets a tighter 1 MiB limit; a manifest that would exceed it is
replaced by an incomplete summary with reason `manifest_too_large` and counts only.

Eligibility rows are recorded by id and row digest rather than as typed references.
Every typed reference is re-validated, including re-hashing its raw body, on each
insert and on every later integrity pass. Typed references are kept to the distinct
fetches, which already cover every eligibility row's sources. See risk R3.

### 3.3 Collector control flow on schema 3

The existing interleaved loop stays exactly as it is for schema 1 and 2. A schema-3
archive takes a separate two-phase path. The run's provenance records
`collection_protocol: "two-phase-v1"`; a run started under the legacy path cannot be
resumed after a migration to schema 3 and must be restarted, consistent with the
existing rule that a resume requires the original code and configuration.

1. **Phase A, discovery.** Status, series, documents, market pages (and the refresh
   scan on resume), schedule, events, and `scope.eligible` for every candidate, each
   decision recorded as today. The schedule is refreshed when its age nears
   `schedule_max_age`, as today. **No book request is made in this phase.**
2. **Summary.** Build the envelope from committed rows and append it in one
   transaction. This is the last step before any book request. A pass that failed in
   Phase A commits an **incomplete** summary with its reasons.
3. **Phase B, books.** Runs only when the committed summary is complete and the command
   is not `discover`. For each eligible contract, in the same order as today:
   - If the schedule behind its latest decision is older than `schedule_max_age − 5`
     seconds, fetch a fresh schedule, re-run `scope.eligible` against the Phase A event
     response and append a **new** eligibility row. That row lies after
     `eligibility_through_id` and is not part of the summary. If it is ineligible, skip
     the book.
   - Request the book with the unchanged `before_attempt` guard (wall-clock T−120 cutoff
     and schedule freshness) and the unchanged budget.
4. **Finish.** `runs.summary_json` keeps every existing field with the existing
   meaning. `discovery_complete` is copied from the committed summary, and
   `discovery_pass_id` and `discovery_digest` are added. Book errors still make the
   run `partial`.

If the summary insert itself fails (disk, lock, deadline), no summary exists, no book
is requested, and the run finishes `failed` with `discovery_summary_not_committed`.
There is no in-process retry.

### 3.4 Independence from books

The summary is committed before the first book request and is protected by PR #7's
update, delete and replace triggers. Phase B never writes to `discovery_passes`. Two
runs with byte-identical discovery evidence therefore produce summaries whose content
differs only in identifiers and timestamps, whether every book succeeds or every book
fails. Constant-baseline eligibility reads only the summary and its receipt (§4.6).

One consequence of the specification's rule is worth stating plainly: a single missing
event response makes the pass incomplete, and an incomplete pass requests **no** books.
Today the collector would still collect books for the other events. The specification
(§3, item 3) chose the stricter rule and this plan follows it. The governing wording,
the distinction from research eligibility, and the alternative are in §9.4.

### 3.5 Recording an incomplete discovery honestly

Checked against `contracts.validate` and `journal.validate_links` at `acc4c24`
**[PR #7]**: every case below is accepted by the existing contract. The contract
requires integers for the bounds and counts, so "unknown" cannot be stored as null.
The record stays honest through `complete=false`, a non-empty `reasons` list (which
the contract already requires), and the manifest flags.

| Failure point | `phase_reached` | Bounds | `counts` | Candidates | `references` |
|---|---|---|---|---|---|
| Rate-limit budget exhausted or request row failed before any attempt completed | `none` | after = through | 0, 0 | Empty, `candidates_known=false` | Empty list (allowed) |
| Status, series or document request failed | `none`, `status` or `series` | after = through | 0, 0 | Empty, not known | The failed attempt and earlier ones |
| Pagination failed, looped or hit a cap | `documents` | after = through | 0, 0 | Those observed so far, not known complete | Pages fetched, including the failed one |
| Schedule unavailable | `pages` | after = through | 0, 0 | Complete list, no decisions | As above plus the failed schedule attempt |
| An event failed or was inconsistent | `schedule` | through > after | Eligible decisions recorded so far | Complete list; some without a decision | As above plus event attempts |
| Deadline or interrupt mid-eligibility | `schedule` or `events` | through ≥ after | As recorded | Complete list; some without a decision | As above |

Rules for writers and readers:

- `counts` and the bounds describe rows that exist. Zero means "no eligible decision
  was recorded", which is true. It never means "the universe has no eligible games".
  Readers must not use `counts` from a pass that is not complete.
- The complete-and-empty case is distinguished by `complete=true`, `phase_reached=done`
  and `candidates_known=true` with an empty candidate list.
- `completed_at` is when the phase ended, whether it succeeded or failed.
- A completed failed attempt (HTTP error, timeout without a body, truncation) is a
  valid typed reference. A pending or recovered-interrupted attempt is not, and goes
  in `incomplete_attempts`.
- The projection (§4.6) accepts a pass as complete only when `complete` is true **and**
  `manifest_version` is known **and** `phase_reached`, `candidates_known` and
  `eligibility_complete` agree with it. Anything else is `universe_incomplete`. A pass
  written with an unrecognized manifest, such as PR #7's test fixture, is therefore
  never usable as a complete universe.

One gap in the existing contract: it requires reasons when a pass is incomplete but
does not forbid reasons when a pass is complete. The follow-up patch adds that check
(§9.6, P4).

## 4. Evidence availability (F3.3b, evidence half)

### 4.1 What a receipt proves

A receipt proves one thing: at `observed_at`, a process other than the writer read the
subject as a committed row. `observed_at` is therefore an upper bound on when the
evidence became available. It proves nothing earlier.

| Value | Why it is not availability proof | Permitted use |
|---|---|---|
| `fetches.retrieved_at` | Sampled at HTTP completion, before the row is committed | Freshness age; selecting what to witness |
| `fetches.started_at` | Request start | Diagnostics |
| Raw body SHA-256 | Identifies bytes, not when they were stored; identical bytes recur | Integrity of the subject |
| `fetches.seq`, row ids | Allocated at request start or insert; order ingestion only | Ordering among already-witnessed records |
| Replay time, replay-created row ids | Replay runs later and can create rows for old fetches | Nothing temporal |
| `discovery_passes.completed_at`, `created_at` | Sampled by the writer before its commit | Freshness age only |
| `runs.finished_at`, `runs.summary_json` | Mutable, sampled before commit | Operations only |
| Provider timestamps | Not local knowledge | Retained as source data |
| A receipt created today | Proves availability today | Cutoffs after today's `observed_at` |

### 4.2 Witness procedure

New module `forecast/witness.py`. One finite invocation:

1. The caller holds the existing `storage.writer_lock`. If it is busy, the command
   fails with `writer_busy`; nothing retries. While the lock is held no cooperating
   writer has an open transaction, so every row the witness can see is committed.
2. Start a run of kind `forecast-witness`. PR #7's health allowlist **[PR #7]** already
   keeps this kind from satisfying collection health, and operations still classifies
   only `kind='collect'`.
3. Sample the session baseline: UTC and monotonic.
4. On a **separate read-only connection**, open a read transaction and select the
   subjects for this batch (§4.3). For each, compute its typed reference, which
   verifies the raw body hash. Close the read transaction.
5. **After** the read has finished, sample UTC and monotonic once. That UTC value is
   `observed_at` for every subject in the batch. Run the clock checks (§4.4).
6. For each subject, append one receipt. `created_at` is sampled just before each
   insert and is never earlier than `observed_at`.
7. Repeat from step 4 while subjects and budget remain, then finish the run with counts.

The read and the observation always precede the insert. A crash between step 5 and
step 6 loses the proof, not the evidence: the next invocation observes at its own,
later, real time. No path accepts a caller-supplied wall time. Tests inject a clock,
and any injected clock forces `mode="synthetic"`.

**This increment is library-only.** It adds no command, no operations hook and no
scheduled caller. Until a caller is implemented (the F3.4 `forecast tick`), no
operational receipt is produced by anything in the repository, and the projection
reports every live input as `availability_unproven`. Tests call the function with an
injected clock, which forces synthetic receipts.

The collector never calls the witness, and the collector module must not import it.
The witness refuses to run under a run of kind `collect`, `discover` or `live-check`.
That keeps the specification's single ordering: the next tick witnesses the previous
pass. No mid-collection or post-book witnessing phase exists.

### 4.3 Subjects, bounds and the receipt record

Subjects in this increment: `discovery_pass` and `fetch`. Binding and publication
subjects belong to later steps.

- Discovery passes are selected first, newest first, at most 100 per invocation, where
  no receipt exists for the pass in this observer namespace and mode.
- Fetches are every **completed** attempt (state `ok`, `parse_error` or `error`, with
  a `retrieved_at`; §9.1) among the newest `witness_scan_rows` (default 5000) by `seq` that has
  no receipt in this namespace and mode. At most `witness_max_subjects` (default 500)
  per invocation, newest first, inserted in ascending `seq`.
- There is no stored cursor. A fetch that was pending and completes later is picked up
  as long as it is inside the scan window. Anything unwitnessed outside the window is
  reported as `outside_window` and stays unproven, which fails closed.
- Hitting either cap, or the time budget, finishes the run `partial` with the remaining
  count. It is never reported as complete.

Receipt envelope against the PR #7 contract **[PR #7]**:

| Field | Value |
|---|---|
| `subject_kind` and its one typed id | `fetch` with `fetch_id`, or `discovery_pass` with `discovery_id` |
| `subject_digest` | For a fetch: the `digest` of its typed reference, which covers the whole row (§9.1; needs patch P1). For a discovery pass: the pass row's `digest`, as PR #7 requires today |
| `references` | Exactly one typed reference to the subject row. For a fetch this fingerprints the full row, including UUID, `seq`, state, raw hash and `retrieved_at`. This is what binds the receipt to exact evidence rather than to bytes alone |
| `observed_at` | Step 5 sample |
| `session_id` | The witness invocation UUID |
| `observer_namespace` | `local-witness-v1` |
| `monotonic_offset` | Seconds since the session baseline, as a decimal string |
| `clock_status` | `trusted` or `untrusted` (§4.4) |
| `mode` | `synthetic` if the subject's lineage is a fixture run or the clock is injected; otherwise `forward_shadow` |
| `run_id`, `code_hash`, `config_hash` | The witness run, `source_hash()`, and the digest of that run's recorded configuration (limits and namespace; §9.2) |

PR #7 keys a receipt by subject, digest, namespace and mode, so each subject has **one**
receipt per namespace, and the first is permanent. Witnessing the same subject again
appends nothing and keeps the original `observed_at`. The witness skips subjects that
already have a receipt; an `IdempotencyConflict` would mean two different observations
for one subject and is reported as an integrity failure, not retried.

### 4.4 Clock rules, detection limits and recovery

Checks made at every observation:

| Check | Trigger | Result |
|---|---|---|
| Divergence | UTC elapsed and monotonic elapsed since the session baseline differ by more than 2 seconds (a step in either direction, or host sleep, during the session) | `untrusted` |
| Regression | `observed_at` is earlier than the high-water mark: the `observed_at` of the most recent **trusted** receipt with the same observer namespace and mode | `untrusted` |
| Negative age | `observed_at` is earlier than the subject's `retrieved_at`, or the pass's `created_at` | `untrusted` |

The high-water mark is looked up among the newest 10,000 receipts. If receipts exist
for that namespace and mode but none of them is trusted, the result is `untrusted`.
Synthetic receipts never contribute to the mark used for `forward_shadow` receipts.

An untrusted observation is still written, as immutable failure evidence. Because the
first receipt for a subject is permanent, that subject stays unproven in this
namespace. Readers treat an untrusted receipt as no proof of availability and report
`clock_untrusted`. The cost is bounded: the evidence could only have served cutoffs in
the following 300 seconds.

**What local clocks cannot establish.** These checks compare the wall clock with
itself and with a monotonic clock that does not survive a restart. They cannot detect:

- A forward step that happens between sessions. It is indistinguishable from elapsed time.
- A backward step between sessions that is smaller than the time since the last trusted
  receipt.
- A constant offset shared by the collector and the witness, or drift below 2 seconds
  per session.

The error is not always conservative. A clock that runs behind true UTC stamps
`observed_at` early, so evidence can appear available before a cutoff it actually
missed. A clock that runs ahead stamps `retrieved_at` late, so evidence can appear
fresher than it is. In both cases the size of the error is bounded by the size of the
undetected skew. A receipt therefore proves ordering and bounds **on the local clock's
timeline**. That this timeline matches true UTC is an assumption about the Mac's time
synchronization, not something this design proves. No external or cryptographic time
attestation exists in v1.

**Recovery, with no operator override.**

| Situation | What happens | How it ends |
|---|---|---|
| Divergence within a session | That session's remaining receipts are untrusted | The next session starts a fresh baseline |
| Wall clock stepped backward | Receipts are untrusted while the clock is below the high-water mark | When the clock is corrected, or when real time passes the mark |
| Wall clock stepped forward, a trusted receipt was written, then the clock was corrected | Receipts are untrusted until real time reaches the future-dated mark | Only by waiting. The outage lasts as long as the size of the jump |

There is no flag, command or configuration value that marks a clock trusted, resets
the high-water mark, or rewrites a receipt. A very large forward jump would stop
trusted witnessing in this namespace for a correspondingly long time. Changing the
observer namespace would start a new mark; that is a reviewed code and protocol change
with its own review, not a recovery procedure, and it is not provided here.

### 4.5 Legacy evidence

Fetches collected under schema 1 or 2, or under schema 3 before this work, have no
receipts and no discovery pass. Nothing in this plan creates either retroactively:

- There is no backfill command, and `discovery_passes` rows can only be produced by a
  live Phase A in the same invocation.
- `replay` and `import_fixture` are unchanged and never write research rows.
- A witness run today can produce receipts for old fetches inside its scan window.
  Their `observed_at` is today, so they cannot qualify for any earlier cutoff.

For a cutoff before the first receipts exist, the projection returns
`availability_unproven` for inputs and `universe_incomplete` for discovery. Such
evidence may appear in a separately labelled diagnostic reconstruction and never in the
strict cohort. The specification's "documented conservative witness" needs an existing
immutable record that observed a run finished; the current archive has none, so this
plan implements no such path.

### 4.6 Read-side availability projection

New module `forecast/evidence.py`, read-only, no clock, no writes. It turns stored rows
into the arguments that the merged F3.1 `forecast.policy` functions already accept.

- `fetch_availability(db, fetch_id, namespace)` returns the fetch's `retrieved_at`
  with the `observed_at` of its trusted receipt, or no observation time.
- `discovery_as_of(db, cutoff, namespace)` returns the applicable pass, its
  `EvidenceTimes(completed_at, observed_at)`, `universe_complete` and reasons.

Applicable-pass rule, using only records whose trusted receipt has `observed_at ≤ C`:

1. Candidates are (a) witnessed discovery passes, and (b) **orphans**: a witnessed
   discovery-stage fetch of a two-phase run whose `seq` is beyond the
   `fetch_seq_through` of every witnessed pass of that run. An orphan means a pass
   started and no summary was committed for it. A crashed pass whose only attempts
   are pending or recovered-interrupted leaves nothing witnessable and is not an
   orphan; it is equivalent to a cycle that never ran, and the earlier pass applies
   only while it is still within 300 seconds (§9.1).
2. The applicable record is the one furthest along in ingestion order (pass `id`, with
   orphans placed by `seq`). Sequence numbers only order records that are already
   witnessed; they never establish availability.
3. Orphan: `universe_complete=False`, reason `discovery_summary_missing`. Incomplete
   pass: `universe_complete=False` with the pass's reasons. Neither falls back to an
   older complete pass.
4. Complete pass: `universe_complete=True`; `policy.freshness` then applies the
   300-second limit to `completed_at` and the cutoff to `observed_at`.
5. A pass or fetch first witnessed after C is not known at C. It neither helps nor
   invalidates the earlier record.

The pass `completed_at` is the freshness anchor for discovery, as the merged F3.1
contract defines. Schedule and market evidence keep their own per-fetch checks.

## 5. Integration and recovery

### 5.1 Transactions and locking

No new lock. Every writer in this plan runs under the existing nonblocking
`writer_lock`, and no network call happens inside any transaction.

| # | Transaction | Owner | Notes |
|---|---|---|---|
| T1 | Start run | Existing | Unchanged |
| T2 | Begin fetch, complete fetch with normalization and checkpoint | Existing | Unchanged |
| T3 | One eligibility row | Existing | Unchanged |
| T4 | **Discovery summary** | New | One `BEGIN IMMEDIATE` through `journal.append`; references re-validated inside it; committed before any book request |
| T5 | Renewed eligibility rows and book fetches | Existing shape | After T4 only |
| T6 | Finish run | Existing | Mutable operational state; proves nothing |
| W1 | Start witness run | New | |
| W2 | Read snapshot | New | Separate read-only connection, closed before the clock is sampled |
| W3 | One receipt | New | One transaction per receipt through `journal.append` |
| W4 | Finish witness run | New | Counts only |

`journal.append` requires an idle connection with foreign keys on and installs a
progress handler for its own duration **[PR #7]**. The collector's connection is idle
between its own transactions, so the summary uses the existing connection.

### 5.2 Idempotency

| Operation | Key | Repeat behavior |
|---|---|---|
| Discovery summary | `(run_id, session_id)` | One attempt per invocation. The same envelope returns the same id; different content is a conflict and is never a replacement |
| Receipt | Subject, digest, namespace, mode | Skipped when present; original time kept |
| Eligibility and fetch rows | Existing | Unchanged |

### 5.3 Failure and recovery

| Event | State left behind | What happens next |
|---|---|---|
| Phase A fails without a crash (page bound, missing event, 401/403, deadline) | Incomplete summary committed with reasons; raw and failed fetches kept | No books this pass. Next invocation is a new pass |
| Operator interrupt during Phase A | Best-effort incomplete summary with `interrupted`; if that write does not happen, no summary | As the next row |
| Crash or kill **before** T4 commits | Fetch rows and checkpoints only; possibly pending fetches; no summary | Once witnessed, those fetches are orphans: discovery incomplete for cutoffs they are the latest record for. No fallback to an older pass |
| Crash **after** T4, before or during books | Summary committed and immutable; run left `running` or `interrupted` | Summary remains valid evidence. Constant eligibility is unaffected |
| Resume of that run | `Store.recover` marks pending fetches interrupted, as today | New `session_id`, fresh discovery, **new** summary row. The earlier row is untouched |
| Book request fails, times out or is truncated | Failed fetch row; run `partial` | Summary unchanged. Midpoint coverage reflects the book result |
| Summary insert fails | No summary; run `failed` | No books. Mutable run diagnostics only, which prove nothing |
| Witness crashes mid-batch | Some receipts written | Next invocation witnesses the rest at its own later time |
| Clock check fails | Untrusted receipts written | Affected subjects unproven |

Failure evidence that is immutable: incomplete summaries, completed failed fetch rows
referenced by them, and untrusted receipts. Failure evidence that is only diagnostic:
`runs.summary_json` and process exit codes.

### 5.4 Schema compatibility and migration

| Path | Schema 1 | Schema 2 | Schema 3 | Other |
|---|---|---|---|---|
| Collection | Legacy loop, unchanged | Legacy loop, unchanged | Two-phase with summary | Rejected |
| Witness and projection | Refused | Refused | Supported | Rejected |
| Migration | Explicit `settlement migrate` | Explicit `forecast migrate` **[PR #7]** | No-op | Rejected |

This plan adds **no migration and no DDL**. Nothing migrates on collection, witness,
health, inspection or replay; `db-init` still creates schema 1. The production
collector is pinned to older code on a schema-1 archive and is not affected by merging
any of this; deployment is a separate decision.

Rollback: stop invoking the witness; run the previous schema-3-capable code. Summaries
and receipts already written stay in place. Nothing is dropped and `user_version` is
never lowered.

## 6. Timing and cohort rules

Unchanged rules this plan must not weaken:

- Freshness stays 300 seconds, inclusive at C, for discovery, schedule, market and
  book evidence. It is not widened to recover coverage.
- The tick runs **before** collection. Evidence from one pass is first witnessed by
  the next tick. No extra witnessing phase is added.
- The collector's own 120-second schedule guard and T−120-second book cutoff are
  unchanged and separate from the research limits.
- Cutoff eligibility, publication or veto status, and settlement eligibility remain
  three separate facts. This increment produces inputs to the first only.
- `forward_shadow`, `historical_reconstruction` and `synthetic` records are never
  pooled. Reconstructed and actual forward results are never pooled.

Worked timeline for the applicable discovery pass. Cycle *k* starts at `t_k = 120k`.
Assume the tick's observation lands 1 second into the cycle and discovery completes
10 seconds after that, so pass *k* completes at `t_k + 11` and is first witnessed at
`t_{k+1} + 1`. Cutoff `C = t_n + x`.

| Offset `x` | No miss | Collection *n−1* missed, ticks ran | Whole cycle *n−1* missed |
|---|---|---|---|
| 0 | Pass *n−2*, age 229, fresh | Pass *n−2*, 229, fresh | Pass *n−3*, 349, **stale** |
| 1 | Pass *n−1*, 110, fresh | Pass *n−2*, 230, fresh | Pass *n−2*, 230, fresh |
| 60 | Pass *n−1*, 169, fresh | Pass *n−2*, 289, fresh | Pass *n−2*, 289, fresh |
| 119 | Pass *n−1*, 228, fresh | Pass *n−2*, 348, **stale** | Pass *n−2*, 348, **stale** |
| 120 | Pass *n−1*, 229, fresh | Pass *n−2*, 349, **stale** | Pass *n−2*, 349, **stale** |

Stale means `policy.cutoff_eligibility` returns `universe_incomplete` and
`discovery:input_stale`, and both baselines abstain. That outcome is accepted. One
missed cycle is not generally tolerated, exactly as the specification says. Book
evidence follows the same arithmetic with its own retrieval times and only affects the
midpoint baseline.

## 7. Acceptance cases

All cases use isolated databases and synthetic evidence. "Summary" means the
`discovery_passes` row; "projection" means §4.6 feeding the merged F3.1 policy.

| # | Case | Expected observable outcome |
|---|---|---|
| A1 | Identical discovery responses; every book succeeds | Summary complete; run `complete` |
| A2 | Same discovery responses; every book times out, errors or is truncated | Summary has the same `complete`, `reasons`, `counts`, candidate list and eligibility decisions as A1; run `partial`; projection gives the same `universe_complete` and the same constant eligibility as A1 |
| A3 | A1 and A2, row inspected before and after books | Summary row bytes identical before the first book request and after the run ends |
| A4 | Any A-series run | Zero book requests sent before the summary row exists |
| B1 | Page limit exceeded, market limit exceeded, repeated cursor | Summary incomplete with the matching reason; zero book requests |
| B2 | One event response fails, mismatches identity, or omits a discovered contract | Summary incomplete with `event_unavailable`, `event_identity_mismatch` or `eligibility_incomplete`; zero book requests; projection gives `universe_complete=False` |
| B3 | Zero open markets, terminal page | Summary complete with zero counts; distinct from B1 |
| B4 | Every candidate ineligible for ordinary reasons | Summary complete; zero books |
| C1 | Fetch retrieved at C−10 s, receipt observed at C+1 ms | `availability_after_cutoff`; not rescued by `seq`, body hash or `retrieved_at` |
| C2 | Response received before C, its commit lands after C | `retrieved_at ≤ C`, receipt necessarily after C; not qualifying |
| C3 | Summary completed before C, first witnessed after C | Not the applicable pass at C; the earlier witnessed pass applies if any |
| C4 | Replay run after C creates normalized rows for a pre-C fetch | No receipt is created; availability unchanged |
| D1 | Process killed before the summary commits, after at least one discovery attempt completed | No summary; fetch rows and checkpoints kept; after witnessing, projection reports `discovery_summary_missing` and does not fall back |
| D1b | Process killed during the first request, leaving only a pending attempt | No summary and nothing witnessable; the earlier pass applies only while within 300 s |
| D2 | Process killed after the summary commits, before books | Summary present and unchanged; projection can use it once witnessed |
| D3 | Process killed during books | As D2 |
| E1 | Witness run twice with nothing new | Second run appends nothing; original `observed_at` kept |
| E2 | Witness killed mid-batch, then rerun | Earlier receipts unchanged; remaining subjects get the later real time |
| E3 | Collection resumed after D1 or D2 | New summary with a new id and `session_id`; earlier summary and receipts unchanged; `phase_sequence` is 1 after D1 and 2 after D2 |
| E4 | Resume of a legacy-protocol run on schema 3 | Refused with instruction to start a new run |
| F1 | Wall clock steps backward between witness runs | Receipts `untrusted`; projection treats subjects as unproven |
| F2 | Host sleep or UTC/monotonic divergence over 2 s within a run | Receipts `untrusted` |
| F3 | Age exactly 300 s, and 300 s plus 1 µs | Fresh, then stale |
| F4 | Newest witnessed pass 349 s old | `universe_incomplete`, `discovery:input_stale`; no older or newer pass substituted |
| G1 | The fifteen cells of the §6 table | Each cell's pass and freshness as tabulated |
| G2 | Discovery durations of 0, 10, 60, 89 s, and 91 s against a 90 s deadline | First four behave as §6 with shifted ages; 91 s leaves no complete summary |
| G3 | Holding discovery evidence fixed, vary only book outcome across the sweep | Discovery columns identical in every combination |
| H1 | Schema-1 or schema-2 archive | Existing collector tests pass unmodified; no research rows; witness refused |
| H2 | Schema-3 archive with fetches from before this work; witness run today | Receipts carry today's time; any earlier cutoff gives `availability_unproven` and `universe_incomplete` |
| H3 | Fixture import of responses with old timestamps | Lineage synthetic; never counted as `forward_shadow`; no summary or receipt imported |
| H4 | Successful witness run after a failed collection | Collection health still unhealthy; operations classification unchanged |
| I1 | Book attempt times out with no body, then is witnessed | A receipt exists whose digest is the fetch row fingerprint; the attempt is visible as the latest completed attempt |
| I2 | Attempt left `pending` by a killed process; witness runs | No receipt; counted as `pending_seen` |
| I3 | That run is resumed, the attempt becomes `interrupted`; witness runs | Still no receipt; counted as `interrupted_seen` |
| I4 | A fetch receipt whose digest is the raw body hash (pre-patch rule) | Rejected on insert; an archive that already holds one fails `journal.integrity` and is not rewritten |
| I5 | Two attempts with byte-identical bodies | Two receipts with different subject digests |
| J1 | Pass whose `config_hash` differs from its run's recorded configuration digest | Rejected |
| J2 | Decision citing a pass without the exact pin, or with a wrong pass digest or collector hash | Rejected |
| J3 | Decision citing a binding with a different research `config_hash`; publication with a different hash from its decision | Rejected, as today |
| K1 | Every allowed and forbidden cell of the §9.3 table | Allowed inserts succeed; forbidden inserts are rejected |
| K2 | Reconstruction decision citing a live pass and live binding | Accepted; cannot be given a publication; cannot enter a `forward-shadow` evaluation |
| K3 | Discovery pass or receipt with mode `historical_reconstruction` | Rejected |
| L1 | Each failure point of the §3.5 table | Summary accepted by the contract with the tabulated values; projection reports `universe_incomplete` |
| L2 | Pass marked complete that carries reasons, or whose manifest flags disagree | Rejected by the patched contract, or unusable in the projection |

## 8. Implementation handoff

### 8.1 Modules and interfaces

| Path | Action | Purpose |
|---|---|---|
| `src/vader_intelligence/collector.py` | Modify | Keep the legacy loop untouched for schemas 1 and 2; add the two-phase schema-3 path |
| `src/vader_intelligence/forecast/discovery.py` | Create | Build the summary envelope from committed rows; no network |
| `src/vader_intelligence/transport.py` | Modify | `Reader.live`: true only when no client, clock, sleep or jitter is injected |
| `src/vader_intelligence/storage.py` | Modify | Schema-version accessor; thin `append_forecast` wrapper that applies the existing free-space check before `journal.append` |
| `src/vader_intelligence/forecast/witness.py` | Create | §4.2–§4.4 |
| `src/vader_intelligence/forecast/evidence.py` | Create | §4.6 |
| `forecast/journal.py`, `forecast/contracts.py` **[PR #7]** | Modify in the separate follow-up patch (§9.6), not in F3.3a | Contracts §9.1–§9.3 |
| `tests/test_collector.py`, `tests/test_integration.py` | Extend | A, B, D, E3, E4, H1 |
| `tests/forecast_storage/test_discovery_pass.py`, `test_witness.py`, `test_evidence.py` | Create | C, E1, E2, F, H2–H4 |
| `tests/forecast/test_timing_sweep.py` | Create | G |
| `docs/handoffs/discovery-witness.md` | Create at implementation time | Actual results, limits |

No change is planned to `cli.py`, `replay.py`, `config.py`, `ops/`, any migration, or
the F3.1 pure modules. `ops/archive.py` compares the stored run summary with the CLI
output and reads fields by name, so additive summary fields do not affect it; this is
to be confirmed by the existing operations tests passing unmodified.

Signatures (declarations only):

```python
# forecast/discovery.py
def build_summary(db, *, run_id, session_id, started_at, completed_at, created_at,
                  eligibility_after_id, fetch_seq_after, config, mode, outcome) -> dict: ...

# forecast/witness.py
def witness(store, *, clock=None, max_subjects=500, scan_rows=5000, budget=15.0) -> dict: ...

# forecast/evidence.py
def fetch_availability(db, fetch_id, namespace="local-witness-v1") -> Availability: ...
def discovery_as_of(db, cutoff, namespace="local-witness-v1") -> DiscoveryEvidence: ...
```

### 8.2 Increments and prerequisites

| Order | Increment | Prerequisites | Done when |
|---|---|---|---|
| 1 | **F3.3a**: two-phase collector and summary | PR #7 cleared and merged; this document's summary contract (§3.2, §3.5, §9.2, §9.3) accepted. **Does not need the follow-up patch** | A, B, D, E3, E4, H1, L1 pass; existing collector, integration and operations suites pass unmodified on schemas 1 and 2 |
| P | Follow-up journal-validation patch (§9.6) | PR #7 merged. Independent of increment 1 | I4, I5, J, K, L2 pass; PR #7's own storage tests pass with updated fixtures |
| 2 | Witness (library only) | Patch item P1; increment 1 for discovery-pass subjects | C4, E1, E2, F1, F2, H3, H4, I1–I3 pass |
| 3 | Availability projection and timing sweep | 1 and 2 | C1–C3, D1 projection, F3, F4, G, H2 pass |
| Later | Bindings, selection, decisions (rest of F3.3b) | Patch items P2 and P3; its own plan | Not in this plan |

Increment 1 is the smallest coherent unit. It writes only discovery passes, and the
values it writes already satisfy every rule the patch adds, so no row written by
increment 1 becomes invalid when the patch lands. Increment 1 and the patch can
proceed in parallel.

### 8.3 Ordered test plan

Tests assert observable behavior: rows present, requests sent, values returned.

1. Schema-1 and schema-2 regression: the existing suites, unchanged.
2. Summary before books: record the order of requests and commits (A4).
3. Book-outcome independence (A1–A3).
4. Incomplete discovery fails closed (B1, B2), and complete-empty and
   complete-all-ineligible are distinct from it (B3, B4).
5. Crash boundaries by killing a subprocess at injected points (D1–D3), then resume
   (E3, E4).
6. Witness basics: one receipt per subject, repeat is a no-op, interrupted batch (E1, E2).
7. Observation ordering: the recorded `observed_at` is never earlier than the end of
   the read and never later than the receipt's `created_at`.
8. Clock anomalies (F1, F2), attempt states (I1–I5).
9. Cutoff boundaries through the projection (C1–C3, F3, F4).
10. Timing sweep on a virtual serial timeline (G1–G3).
11. Legacy and synthetic lineage (H2, H3), health separation (H4), and the
    configuration and mode link tables (J, K) once the patch exists.
12. Lint, format, full suite, operations suite, wheel build and installed-wheel smoke,
    on the Mac, in a disposable checkout and database.

### 8.4 Completion criteria

- Every case in §7 passes natively on the Mac; nothing is claimed from portable runs only.
- A test proves no book request precedes the summary commit.
- A test proves the collector module does not import the witness.
- Existing schema-1 and schema-2 collector, integration and operations tests pass with
  no edits.
- Measured: summary size, summary insert time, receipts per pass, witness time per
  pass, and `journal.integrity` time on a synthetic archive of at least 720 passes.
- Handoff document records actual results and separates offline from live evidence.
- No production archive touched, no migration run on it, nothing deployed.

## 9. Interface contracts resolved against PR #7 at `acc4c24`

Revision 1 listed three blockers. This section replaces them with contracts. Nothing
here is implemented, and nothing here modifies PR #7. None of it needs DDL, but each
item is still a compatibility change for any schema-3 archive that already holds the
affected rows; §9.6 states that boundary.

### 9.1 Fetch receipts: canonical subject identity

Attempt states, from `storage.py` and `transport.py` on `main`:

| State | How reached | `retrieved_at` | Body | Can still change | Receipt subject |
|---|---|---|---|---|---|
| `pending` | Row inserted before the request is sent | NULL | None | Yes | No |
| `interrupted` | `Store.recover` on resume of a crashed run | NULL | None | No | No |
| `error` | Completed: HTTP non-200, transport failure, deadline, byte-limit truncation, unsupported encoding, in-process interrupt | Set | Present or absent | No | **Yes** |
| `parse_error` | Completed: HTTP 200 body that failed normalization | Set | Present | No | **Yes** |
| `ok` | Completed and normalized | Set | Present | No | **Yes** |

A **completed attempt** has a `retrieved_at` and state `ok`, `parse_error` or `error`.
This is the same precondition `references.reference` already enforces, and it matches
the specification's "latest attempted completed book retrieval" (§4). Replay does not
modify `fetches` rows.

Contract for a receipt with `subject_kind="fetch"`:

1. Let `R = reference(db, "fetches", {"id": fetch_id})`, the existing typed reference
   **[PR #7]**, unchanged. `R` carries the fetch UUID, a digest over the **entire**
   row (sequence, request, attempt number, start and retrieval times, HTTP status,
   headers, body hash or NULL, state, error text, truncation flag, envelope and source
   API versions), the request stage, and the source lineage. When a body exists it
   also verifies the stored bytes against the hash.
2. `subject_digest` must equal `R["digest"]`.
3. `references` must equal `[R]`, exactly one element.
4. A trusted receipt's `observed_at` must not precede `retrieved_at` (retained).

For the other three subject kinds nothing changes: `subject_digest` is the subject
row's `digest` column.

The typed reference is reused rather than a second canonical form, so there is one
fingerprint definition, it is already re-validated on every integrity pass, and it
works with or without a body. The row digest includes `elapsed_seconds`, a floating
point column; every typed fetch reference in PR #7 already depends on it, so this adds
no new exposure, but it does mean a restore path must preserve that value exactly, as
the SQLite backup mechanism does.

**Pending, interrupted and later-completed attempts.**

- Neither is ever a receipt subject. A receipt is written only against a completed
  row, and a completed row never changes, so no legitimate write can make a receipt's
  digest stale.
- The witness holds the writer lock. A `pending` row it can see therefore belongs to a
  process that no longer holds the lock: a crashed or killed attempt. That row can
  change only through `Store.recover` on a resume, which makes it `interrupted`. It
  cannot later become a completed retrieval.
- The general rule holds anyway: nothing is recorded for a row while it is pending. If
  a row is later found completed inside the scan window, it is witnessed then, with
  that run's real time. Nothing is backdated.
- Both states are reported: the witness run counts `pending_seen` and
  `interrupted_seen`, and a discovery summary lists them under `incomplete_attempts`.
  For readers they are, in the specification's words, reported but not a completed
  snapshot.
- Consequence: a crashed pass with no completed attempt leaves nothing to witness and
  cannot mark discovery incomplete. That is the same state of knowledge as a cycle
  that never ran, and the 300-second limit bounds how long the earlier pass can apply.
  A crashed pass with at least one completed discovery attempt is an orphan (§4.6).

**Effect of changing the digest semantics.**

| Area | Effect |
|---|---|
| Validation | `validate_links` changes for fetch subjects: rule 2 replaces the body-hash comparison, and rule 3 is new. `contracts.validate` is unchanged; the digest is still 64 hex characters |
| Existing receipts | PR #7's shipped fixture creates one receipt, for a publication, which is unaffected. Any **fetch** receipt written under the body-hash rule fails the new validation |
| Integrity | `journal.integrity` raises on such a row. That fails `Store.verify_integrity`, schema-3 `replay`, and operations inspect, backup and restore validation for that archive |
| Repair | None in place. Receipts are immutable by trigger. The archive must be preserved as it is and a new one built from a schema-2 backup, the remedy PR #7 already documents for its pre-fix layout |
| Detection | Not structural. `require_schema` cannot see it; only the integrity pass does |
| Idempotency | The key formula is unchanged, but it includes the digest. The same fetch therefore has a different key under each rule, and an old and a new receipt could coexist; the old one remains invalid. One receipt per subject then rests on digest stability, which holds because subjects are completed rows |
| Dual acceptance | Rejected. Accepting either digest would keep receipts that bind bytes only |

Landing condition: no retained schema-3 archive may hold fetch receipts when the patch
lands. Nothing in the repository can produce one today, since no witness exists.
Production is reported to be schema 1; that was not verified here.

### 9.2 Configuration provenance

Three different configurations exist. Each hash is the digest of the writing run's
recorded `provenance_json.configuration`, so it can be recomputed from the archive.

| Configuration | Written by | Carried by |
|---|---|---|
| **Collector** (`Config`: limits, timeouts, schedule age, pregame buffer, database path) | `collect`, `discover`, `live-check` | `discovery_passes.config_hash` |
| **Observer** (witness limits and namespace) | The witness run | `forecast_receipts.config_hash` |
| **Research protocol** (specification §2 defaults; parser arrives in F3.4) | Tick, reconstruct, evaluate | `config_hash` of `forecast_bindings`, `forecast_decisions`, `forecast_publications`, `evaluation_runs`, `evaluation_items` |

Compatibility rule for every parent-child link:

| Child → parent | Rule | Against PR #7 |
|---|---|---|
| Discovery pass → its run | `config_hash` equals the digest of the run's recorded configuration; run kind is `collect`, `discover`, `live-check` or `fixture` | New, additive |
| Receipt → its run | `config_hash` equals the digest of the run's recorded configuration | New, additive |
| Receipt → subject | No configuration relation. A receipt records an observation and is valid under any protocol | Unchanged |
| Binding → previous binding | Same `config_hash`, `policy_version`, `candidate_key` | Retained |
| Decision → binding | Same research `config_hash`; identity fields equal | Retained |
| Decision → discovery pass | **Exact pin instead of equality.** `manifest.discovery` must equal `{"id": discovery_id, "digest": <pass row digest>, "collector_config_hash": <pass config_hash>}`. The existing rule that an eligible decision needs a complete pass is retained | Changed |
| Publication → decision | Same research `config_hash`; decision digest equal | Retained |
| Evaluation item → evaluation run | Same `config_hash` and run | Retained |
| Evaluation item → decision | Same dataset, protocol and `config_hash` | Retained |
| Evaluation correction → previous | Same dataset, protocol, `config_hash`, view and population | Retained |

Only one check changes, and it is replaced, not removed. Equality between a decision
and its pass cannot be satisfied honestly: the pass is written by the collector, which
does not know the research protocol and must not depend on it, and a protocol change
would otherwise strand all earlier discovery. The pin keeps exact provenance: each
decision names the precise pass and the precise collector configuration its universe
came from. Every same-protocol check between research records stays.

A matching rule for research records against their own run (binding, decision,
publication and evaluation `config_hash` equal to the run's configuration digest)
belongs with the F3.4 research configuration parser and is not part of this patch.

### 9.3 Mode relationships

`mode` currently mixes two properties. **Lineage** is synthetic or real. **Cohort** is
forward (`forward_shadow`) or reconstruction (`historical_reconstruction`). Evidence
records have lineage only; research records have both.

Evidence records are fetches (lineage from the run kind), discovery passes and
receipts. For a discovery pass or receipt, `forward_shadow` means "produced by a live
process using system clocks". It is not a claim of publication. Neither may be
`historical_reconstruction`, because a reconstruction cannot create a past pass or a
past observation.

Abbreviations: S synthetic, F `forward_shadow`, H `historical_reconstruction`. Pairs
are child → parent.

| # | Relationship | Allowed | Forbidden | Against PR #7 |
|---|---|---|---|---|
| M1 | Any record → its writing run | Any mode on a non-fixture run; S on a fixture run | F or H record on a fixture run | Retained |
| M2 | Any record → typed reference, at any depth | F or H record → real source of either cohort; S record → any source | F or H record → synthetic source | Retained |
| M3 | Discovery pass, own mode | F, S | H | New |
| M4 | Receipt, own mode | F, S | H | New |
| M5 | Receipt → fetch | S → synthetic fetch; F → real fetch; S → real fetch (injected clock, separate key, never proof) | F → synthetic fetch | Retained |
| M6 | Receipt → discovery pass, receipt → publication | S → S, F → F | Everything else | Retained |
| M7 | Receipt → binding | S → S, F → F | Any receipt on an H binding | Retained in effect |
| M8 | Binding → previous binding | Same mode | Everything else | Retained |
| M9 | Decision → binding | S → S, F → F, **H → F**, H → H | F → H; F or H → S; S → F or H | H → F is new |
| M10 | Decision → discovery pass | S → S, F → F, **H → F** | F or H → S; S → F | H → F is new |
| M11 | Decision → superseded decision | Existing rules: the correction is not F, uses a distinct dataset, same opportunity | Synthetic ancestry under a real correction, at any depth | Retained |
| M12 | Publication → decision | S → S, F → F | Any H publication; any publication for an H decision | Retained |
| M13 | Evaluation run, view against mode | `cutoff-reconstruction` with H; `forward-shadow` with F; either view with S | Any other pairing | Retained |
| M14 | Evaluation run → superseded run | Same mode and cohort | Everything else | Retained |
| M15 | Evaluation item → its evaluation run, decision, publication, publication receipt | Same mode | Any cross-mode link | Retained |

What the table guarantees:

- **Reconstruction may cite live evidence.** An H decision can reference a live pass
  and a live binding through M9 and M10, and live receipts as typed references through
  M2. It still cannot qualify as forward publication: M12 forbids a publication for it,
  and M13 with M15 keep it out of every `forward-shadow` evaluation.
- **Synthetic ancestry never becomes real evidence.** M1, M2, M5 and M11 block it at
  the run, the reference, the receipt and the correction chain, and every foreign-key
  link forbids a real child of a synthetic parent.
- **Scoring populations stay separate.** The decision idempotency key includes the
  mode, so a forward decision and a reconstructed decision for the same game are two
  rows. M15 stops either from being scored in the other's evaluation, and M13 fixes
  which view each evaluation may use.
- **A forward decision cannot cite a reconstruction binding** (M9), because such a
  binding may have been prepared after the fact.

Only M9 and M10 relax anything, and only for a reconstruction child of live evidence.
M3 and M4 are new restrictions.

### 9.4 Discovery failure policy

Governing wording, from the specification:

- §3, item 3: "Only a complete committed phase enables its eligible candidates' book
  stage."
- §3, item 1: "Phase failure or cap exhaustion is explicitly incomplete."
- §3, item 2: "Membership ambiguity/missing sources fail the affected discovery scope
  closed."
- §8 test matrix: "Partial pages, ambiguous membership, missing eligibility decisions,
  phase interrupted before commit | No eligible complete-discovery assertion; no books
  before phase commit; constant and midpoint fail closed for affected scope".

**Retained for this increment:** on schema 3, an incomplete discovery pass requests no
books. v1 has one discovery scope per pass, so the affected scope is the whole pass.

Two different things are involved, and the policy above settles only how they are
coupled in this increment:

| | Research-cohort eligibility | Collecting a book |
|---|---|---|
| Question | May a game at cutoff C enter the strict cohort? | Is it safe to request and archive this contract's book now? |
| Depends on | The applicable pass being complete, witnessed by C and fresh | That contract's own pregame proof: its eligibility decision, schedule freshness and the T−120 guard |
| If discovery is incomplete | Both baselines abstain for cutoffs where that pass applies | The book's own proof may still be valid |

So an incomplete pass can contain contracts whose books would be individually safe to
collect, and the approved rule declines to collect them. The research cost of that is
close to nil: where the incomplete pass is the applicable one, the midpoint abstains
regardless, and a later complete pass brings its own fresher books. The cost falls on
the archive and on operations: a gap in raw book history, a stale `last_book_at` in
health, and a failed collection cycle, all from one missing event response.

**Separate proposal, not adopted here.** Allow books for individually eligible
contracts after an *incomplete* summary has been committed.

- Requires amending specification §3 item 3 and the §8 matrix row above, with review.
- Would not change cutoff eligibility: an incomplete pass still fails the common gate.
- Such a book could later be the latest completed attempt for a cutoff governed by a
  later complete pass. The specification's book requirements (§4) rest on the book's
  own eligibility evidence, so this is defensible, but it must be decided explicitly.
- Would restore archive continuity and avoid failed cycles for a single bad event.
- Would make "no books after incomplete discovery" stop being an invariant that tests
  and operators can rely on.

Until that proposal is separately reviewed and the specification is amended, the
retained rule stands and the implementation must enforce it (cases B1, B2).

### 9.5 Witness and clock scope

Stated in full in §4.2 and §4.4. In summary: the witness is a library function with no
caller in this increment; no operational receipt exists until F3.4 supplies one; clock
checks detect in-session divergence, a backward step below the last trusted receipt
and negative ages; they cannot detect a forward step between sessions, a small
backward step or a shared offset; recovery is by a new session or by waiting; there is
no trust override.

### 9.6 Smallest follow-up interface patch

To be written **after** PR #7 is cleared and merged, as its own reviewed change. It is
not implemented here and PR #7 is not to be modified for it.

Files: `forecast/journal.py`, `forecast/contracts.py`, and
`tests/forecast_storage/` (fixtures and cases). No SQL, no migration, no
`user_version` change.

| Item | Change | Needed before |
|---|---|---|
| P1 | Fetch receipt subject: rules 2 and 3 of §9.1 in `validate_links` | The witness (increment 2) |
| P2 | Decision → discovery pass: exact pin replaces `config_hash` equality (§9.2) | Any decision is written |
| P3 | A mode-compatibility function replaces strict equality for decision → binding and decision → discovery pass (M9, M10); `contracts.validate` rejects H for discovery passes and receipts (M3, M4) | Any reconstruction decision is written |
| P4 | Additive checks: pass → run and receipt → run configuration digest; run kind for a pass; a complete pass carries no reasons | The witness, so that live rows are checked from the first one |

The smallest patch that unblocks this plan is **P1 alone**. All four are recommended
as one change, for one reason: each alters what a valid schema-3 row is, and one
compatibility boundary is easier to reason about and to document than several.

Compatibility boundary of the patch, stated explicitly:

- Schema 3 is still a draft. Production is reported to be schema 1.
- After the patch, an archive written by pre-patch code is valid only if it holds no
  fetch receipts (P1), no decision that cites a discovery pass (P2), and no pass or
  receipt whose `config_hash` differs from its run's configuration digest (P4).
  PR #7's own test fixture fails P2 and P4 as written and must be updated in the patch.
- A non-conforming archive is rejected by the integrity pass, never rewritten, and is
  handled as PR #7 already prescribes for its pre-fix layout: preserve it, and build a
  new archive from a schema-2 backup.
- Rows written by increment 1 conform by construction, before or after the patch.

## 10. Decisions

### 10.1 Resolved in revision 2

| # | Question | Resolution |
|---|---|---|
| 1 | How is a failed fetch without a body witnessed? | The receipt binds the typed fetch reference, whose digest covers the whole completed row (§9.1) |
| 2 | Are pending and interrupted attempts witnessed? | No. Reported, never subjects (§9.1) |
| 3 | What happens to receipts written under the body-hash rule? | Invalid after the patch; no dual acceptance; archive preserved and rebuilt (§9.1, §9.6) |
| 4 | Which configuration does each record carry? | Collector for passes, observer for receipts, research protocol for the rest (§9.2) |
| 5 | Is configuration validation removed anywhere? | No. One equality is replaced by an exact pin; two run-link checks are added (§9.2) |
| 6 | May a reconstruction cite live evidence? | Yes, through M9, M10 and M2, and it can never be published or scored as forward (§9.3) |
| 7 | Are books collected after incomplete discovery? | No, per specification §3 item 3. The alternative is a separate proposal (§9.4) |
| 8 | Does this increment produce operational receipts? | No. Library only (§4.2) |
| 9 | Is there a clock trust override? | No (§4.4) |
| 10 | Can the PR #7 contract record an incomplete discovery, including failure before eligibility? | Yes, with the conventions of §3.5; one missing check is added by P4 |
| 11 | What does F3.3a need? | Merged PR #7 and this contract. Not the patch (§8.2) |

### 10.2 Unresolved policy questions

These are not decided here because each would add or change a research rule.

| # | Question | What this plan assumes until decided |
|---|---|---|
| Q1 | Is a change of **collector** configuration inside one protocol cohort "configuration drift within the cohort", which specification §2 says to reject? PR #7's equality check implied yes. The pin in §9.2 records the collector configuration on every decision but does not reject a change | Not drift. Each decision pins its collector configuration, and evaluation reports must list the distinct collector hashes in a cohort |
| Q2 | Should the HTTP `Date` header already archived with every fetch be used as an external sanity bound on the local clock, marking receipts untrusted when they disagree? It would catch offsets that local clocks cannot (§4.4), but it adds an eligibility-affecting rule based on a provider value | Not used. Local checks only |
| Q3 | Should books be collected for individually eligible contracts after an incomplete discovery (§9.4)? | No. Approved policy retained |

Routine choices made from the code and specification, listed so a reviewer can
disagree: `phase_sequence` as the per-run summary ordinal; `counts` as eligible
decisions recorded; eligibility rows recorded by id and digest instead of typed
references; non-two-market events stay fail-closed; one observation sample per read
batch; newest-first bounded witness scan with no stored cursor; untrusted receipts are
written and are permanent; the regression mark is kept per namespace and mode;
resuming a legacy-protocol run on schema 3 is refused.

## 11. Failure modes, hazards and risks

Hazard identifiers are from the pinned data-systems catalog.

| Hazard | Where it applies | Mechanism |
|---|---|---|
| H-02 check-then-act | Summary built from reads, then inserted | Single writer lock; typed references re-validated inside the insert transaction |
| H-03 uniqueness | One summary per invocation, one receipt per subject | Database unique keys and replace triggers **[PR #7]** |
| H-06 side effect in transaction | Summary and receipt inserts | No network inside any transaction |
| H-07, H-08, H-42 unbounded work | Witness scan, manifest | Row window, subject cap, time budget, 1 MiB manifest limit |
| H-09 breaking schema change | Collector path switch | Additive; gated on schema 3; legacy path untouched |
| H-14 non-idempotent retry | Summary, receipts | Deterministic keys; conflict instead of replacement; no in-process retry |
| H-18 ambiguous outcome | Crash around the summary commit | Row presence is the only truth; resume writes a new pass |
| H-20, H-21, H-36 clocks | Receipts, freshness | UTC for cutoffs, monotonic for elapsed, explicit clock status, sequence for order only |
| H-38 destructive backfill | Legacy evidence | No backfill path exists |
| H-39 overlapping jobs | Witness and collector | Same nonblocking writer lock; finite commands |
| H-46 rollback | Deployment | Compatible-code rollback; nothing dropped |
| H-48 untested failure paths | Crash, clock, bounds | §7 D, E, F cases |

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Two-phase ordering lengthens a pass or adds schedule refreshes, eating into the 90-second wrapper deadline | Medium | Missed books, stale evidence | Measure in increment 1; do not change the deadline or cadence here |
| R2 | Per-receipt durable commits make the witness too slow to clear a pass inside the tick budget | Medium | Partial witnessing, unproven evidence | Measure; cap and report; batch insert is a possible later journal change |
| R3 | `journal.integrity` re-validates every reference of every pass, so its time grows with history (about 720 passes a day at the current cadence) and it runs inside replay, inspection and backup | Medium | Operations commands slow down over weeks | Typed references limited to distinct fetches; measure at 720 passes; revisit before long-running use |
| R4 | A single flaky event endpoint blanks books for a pass (§9.4) | Medium | Lower midpoint coverage | Accepted by specification; reported, not softened |
| R5 | Clock checks miss a skew they cannot see (§4.4) | Low | Evidence admitted or aged wrongly by up to the size of the skew | Stated residual risk; no cryptographic or external time claim; Q2 |
| R6 | The follow-up patch lands after some schema-3 archive already holds affected rows | Low | That archive fails integrity and must be rebuilt | Land the patch before the witness exists; §9.6 boundary |

## 12. Review record for this document

Revision 1 checked against the specification: §2 visibility receipts and clock check,
§3 collector phase boundary and population rules, §6 table definitions and
transactions, §7 scheduling boundary, §8 step table and test matrix, §9 examples, §10
open decisions. Against `main` source: `collector.py`, `storage.py`, `transport.py`,
`normalize.py` (book eligibility proof), `scope.py`, `replay.py`, `config.py`,
`cli.py`, `forecast/policy.py`, `ops/archive.py` (result classification), migration 001.
Against PR #7 at `acc4c24`, read-only: `forecast/journal.py`,
`forecast/contracts.py`, `forecast/references.py`, `forecast/schema.py`, migration 003,
the `storage.py`, `cli.py`, `replay.py` and `ops/archive.py` diffs, and the storage handoff.

Revision 2 rechecked, for the contracts in §3.5 and §9: fetch state transitions in
`Store.begin_fetch`, `complete_fetch` and `recover` and the interrupt path in
`Reader.get`; the preconditions of `references.reference`; every `parent(...)` call
and every `config_hash` comparison in `journal.validate_links`; the discovery-pass and
receipt branches of `contracts.validate` and `contracts.key`; and which receipts
PR #7's test fixtures create. The quoted specification wording in §9.4 was copied from
§3 and §8 of the specification on `main`.

The §6 table was computed by hand and then reproduced by a throwaway script that
models only the stated assumptions (pass completion at `t_k + 11`, first observation
by the next tick that runs). That script is not part of the repository and is not a
test of any implementation.

Not done: no application test was run and no database was created. The contracts in
§9 were derived by reading code; none was exercised against PR #7.
