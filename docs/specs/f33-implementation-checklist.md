# F3.3 implementation checklist: requirement → code → test

Status: living checklist kept with the implementation branches so a reviewer can trace
each governing requirement to the code that implements it and the test that proves it.
Written 2026-10-12 by the implementing session against `main` `ebb58df` with PR #8
(`deea149`, documentation only) as the base of the stacked branches.

Governing documents: [forecast/evaluation specification](forecast-evaluation-v1.md) and
the [F3.3 implementation plan](f33-implementation-plan.md), revision 3. Spec wins over
plan; plan wins over this checklist; this checklist records what was built.

Branch structure (stacked, nothing merged by the implementer):

| Branch | Base | Content |
|---|---|---|
| `docs/f33-implementation-plan` (PR #8) | `main` | Plan and spec amendment, unchanged by this work |
| `feat/f33a-discovery-boundary` | PR #8 head | Increment 1: two-phase collection and discovery summaries (this file, part A) |
| `feat/f33-record-contract-2` | `feat/f33a-discovery-boundary` | Patch P, witness, projection, evaluation integration (part B) |

Legend: **Code** is the implementing module; **Test** is the proving test; ✅ implemented
and tested on this Mac; ⬜ not in this increment (see the plan for where it belongs).

## Part A: F3.3a discovery boundary (`feat/f33a-discovery-boundary`)

| # | Requirement (source) | Code | Test | State |
|---|---|---|---|---|
| A-1 | Schema 1/2 keep the interleaved loop unchanged (spec §3, plan §3.3) | `collector.py:Collector._run_legacy` (moved verbatim) | `tests/test_collector.py`, `tests/test_integration.py` unmodified; `test_discovery_boundary.py::test_legacy_schemas_keep_the_interleaved_loop_and_write_no_research_rows` | ✅ |
| A-2 | On schema ≥3, Phase A (status, series, documents, pages, schedule, events, eligibility) makes no book request (spec §3 item 1) | `collector.py:Collector._discover` | `::test_complete_run_commits_summary_before_any_book` (A4 ordering), every B/L case asserts zero orderbook requests | ✅ |
| A-3 | One immutable `discovery_passes` row per invocation, committed before the first book (spec §3 item 2, plan §3.2) | `forecast/discovery.py:build_summary`, `storage.py:Store.append_forecast`, `collector.py:_run_two_phase.commit_summary` | `::test_complete_run_commits_summary_before_any_book`, `::test_interrupt_after_summary_keeps_it_and_resume_is_phase_two` | ✅ |
| A-4 | Completeness evidence E1–E9 with the fixed reason order (plan §3.1) | `discovery.py:REASON_ORDER`, `STAGE_REASONS`, `build_summary`; `transport.py:PaginationError` | `::test_pagination_bounds_commit_incomplete_summary_without_books`, `::test_event_failures_fail_closed_with_no_books`, `::test_failure_points_are_recorded_honestly`, `::test_series_identity_mismatch_is_its_own_reason`, `::test_access_denied_during_discovery_fails_closed` | ✅ |
| A-5 | Incomplete discovery requests no books (spec §3 item 3, plan §9.4, operator decision Q3) | `collector.py:_run_two_phase` (only `summary["complete"]` enters `_books`) | all B cases, `::test_operator_interrupt_during_discovery_writes_incomplete_summary` | ✅ |
| A-6 | **Enforced at the persistence boundary**, not only by caller convention (assignment §2) | `storage.py:Store._require_complete_discovery` inside `Store.request` for stage `book`; `DiscoveryGateError` | `::test_store_refuses_books_without_a_complete_summary_for_this_invocation`, `::test_bypassing_caller_cannot_retain_books_after_incomplete_discovery` | ✅ |
| A-7 | Previously retained valid history preserved; summary never updated on resume (spec §6 `discovery_passes`) | PR #7 triggers; `build_summary` phase_sequence; new session per resume | `::test_interrupt_after_summary_keeps_it_and_resume_is_phase_two` (row bytes equal), `::test_kill_before_summary_leaves_no_summary_and_resume_starts_a_new_pass` | ✅ |
| A-8 | Empty complete discovery distinct from incomplete/unknown scope (spec §3 item 2, plan §3.5) | `build_summary` (`complete`, `candidates_known`, `phase_reached=done`, empty candidates) | `::test_empty_terminal_discovery_is_complete_and_distinct_from_failure`, `::test_all_candidates_ineligible_is_complete_with_zero_books` | ✅ |
| A-9 | Failure points recorded honestly, incl. pending/interrupted attempts not typed references (plan §3.5, §9.1) | `build_summary` (`incomplete_attempts`, `COMPLETED_STATES`) | `::test_failure_points_are_recorded_honestly`, `::test_operator_interrupt_during_discovery_writes_incomplete_summary` | ✅ |
| A-10 | Book outcome never changes the summary (spec §3 item 3, plan §3.4, cases A2/A3) | Phase B never writes `discovery_passes`; triggers | `::test_book_outcome_never_changes_the_summary[error,timeout,truncated]`, `::test_403_on_a_book_still_fails_the_run_after_the_summary` | ✅ |
| A-11 | Phase B keeps fresh-schedule and T−120 guards; renewed proof appended separately (spec §3 item 3, plan §3.3 step 3) | `collector.py:Collector._books` | `::test_phase_b_renews_stale_schedule_proof_outside_the_summary`, `::test_cutoff_crossing_during_two_phase_discovery_is_inconclusive` | ✅ |
| A-12 | Summary insert failure → run `failed`, `discovery_summary_not_committed`, no books (plan §3.3) | `_run_two_phase` | `::test_summary_insert_failure_fails_the_run_without_books` | ✅ |
| A-13 | Crash before commit → no summary, raw rows kept; resume gets new session/pass; `phase_sequence` 1 after D1, 2 after D2 (plan §5.3, E3) | `Store.recover` (existing), `build_summary` | `::test_kill_before_summary_leaves_no_summary_and_resume_starts_a_new_pass` (real SIGKILL subprocess), `::test_kill_before_first_request_leaves_nothing_witnessable`, `::test_interrupt_after_summary_keeps_it_and_resume_is_phase_two` | ✅ |
| A-14 | Legacy-protocol run cannot be resumed on schema 3 (plan §3.3, E4) | `collector.py:Collector.run` (provenance `collection_protocol`) | `::test_legacy_protocol_run_cannot_be_resumed_on_schema_three` | ✅ |
| A-15 | Mode is lineage: synthetic whenever transport or clocks are injected (plan §3.2, §9.3 M3) | `transport.py:Reader.live`, `_run_two_phase` | `::test_reader_lineage_flag`; every test asserts `mode == "synthetic"` | ✅ |
| A-16 | `config_hash` is the digest of the run's recorded collector configuration (plan §9.2) | `discovery.py:run_config_hash` | `::test_complete_run_commits_summary_before_any_book` (payload validated by journal, integrity verified) | ✅ |
| A-17 | No network inside a write transaction; summary uses the collector's idle connection (plan §5.1) | `Store.append_forecast` → `journal.append` (own BEGIN IMMEDIATE) | `::test_complete_run_commits_summary_before_any_book` via `verify_integrity`; `journal` refuses an open transaction (existing `test_no_nested_commit_or_missing_fk_enforcement`) | ✅ |
| A-18 | Discovery outcome inspectable through existing operational interfaces (assignment §2) | `Store.health()["latest_discovery"]`; run summary adds `discovery_pass_id`, `discovery_digest`, `discovery_reasons`; `ops/archive.classify` unchanged (`discovery_no_eligible_games` vs `failed_or_partial`) | `::test_complete_run_commits_summary_before_any_book`, `::test_empty_terminal_discovery_is_complete_and_distinct_from_failure`, `::test_live_check_summary_round_trips_through_cli_finish` | ✅ |
| A-19 | Collector never imports the witness (plan §4.2, §8.4) | no import | `::test_collector_does_not_import_the_witness` | ✅ |
| A-20 | Existing operations suite passes unmodified (plan §8.1) | — | `tests/ops/test_operations.py` (31 run, 1 skipped) | ✅ |
| A-21 | Manifest ≤1 MiB, otherwise `manifest_too_large` with counts only (plan §3.2) | `discovery.py:MANIFEST_LIMIT` | not exercised by a test (would need >1 MiB of candidates; max_markets caps at 100) | ⚠️ code only |
| A-22 | Measured: summary size, insert time, integrity time at 720 passes (plan §8.4) | — | `docs/handoffs/discovery-boundary.md` records the synthetic measurement | see handoff |

Routine implementation choices made from the plan and recorded here for reviewers:

- `eligibility_incomplete` is added only for candidates left undecided by an event
  that answered without covering them, or when nothing else explains the gap. A
  phase-ending failure (`schedule_unavailable`, `access_denied`, `deadline_exhausted`,
  `interrupted`, …) explains every later missing decision and is reported alone.
- `phase_reached = done` requires every discovered candidate to have a decision and no
  reason; an event that omits a discovered contract leaves the pass at `events`.
- Deadline exhaustion inside the event loop ends Phase A (E8) rather than skipping one
  event; the pass is incomplete either way.
- Run status: a stage failure before or at the schedule is `failed`; event-level
  incompleteness is `partial`; both commit an incomplete summary and request no books.
  `errors` always carries at least one line when the summary is incomplete.
- The book gate parses the invocation session from the existing `session:ticker`
  request key and requires the **latest** committed pass of the run to be that session's
  and complete, so a resumed invocation cannot borrow an earlier session's summary.

## Part B: record contract 2, witness, projection, evaluation integration (`feat/f33-record-contract-2`)

Filled in by the second branch; see that branch's revision of this file.
