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
| A-12 | Summary insert failure → run `failed`, `discovery_summary_not_committed`, no books (plan §3.3) | `_run_two_phase` | `::test_summary_insert_failure_fails_the_run_without_books`, `::test_summary_free_space_guard_fails_the_run_without_books` | ✅ |
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

Stacked on `feat/f33a-discovery-boundary`. Implements the plan's follow-up patch P
(§9.7), the witness and projection (increments 2 and 3), and the cohort rules that the
F3.5 evaluation plan will build on. Nothing merged.

| # | Requirement (source) | Code | Test | State |
|---|---|---|---|---|
| B-1 | Record contract version discriminator in the payload; absent = contract 1 frozen; 2 for receipts and decisions only; anything else rejected (plan §9.6) | `contracts.py:version`, `validate` | `test_record_contract.py::test_unknown_contract_versions_are_rejected_on_insert`, `::test_exact_retry_of_contract_one_returns_original_on_both_schemas` | ✅ |
| B-2 | Contract 2 only on schema 4; explicit migration 004 changes no table and no row; plain `forecast migrate` is 2→3 and a no-op at 3 and 4; `--record-contract 2` performs 004; never downgrades (plan §9.6, N11) | `migrations/004_record_contract_2.sql`, `schema.py:migrate`, `cli.py` | `::test_explicit_migration_004_touches_no_row`, `::test_record_contract_flag_requires_schema_three`; CLI exercised on a disposable archive | ✅ |
| B-3 | Contract 2 closed to new contract-1 receipts/decisions on schema 4; contract 1 stays valid, inspectable and counted (plan §9.6, N1, N4, N6) | `journal.py:require_contract_schema`, integrity counts | `::test_contract_two_rejected_on_schema_three_and_contract_one_closed_on_four`, `::test_legacy_fetch_receipt_stays_valid_under_contract_one_and_counted`, `::test_mixed_archive_counts_each_row_under_its_own_contract` | ✅ |
| B-4 | Validate the archive's supported schema and contract before enabling WAL or any mutating init (Store contract) | `storage.py:_validated_schema_version` accepts 3 and 4 and runs `check_schema` before WAL (unchanged position) | `tests/forecast_storage/test_store_initialization.py` (schema-4 handling), `::test_binary_at_main_refuses_a_schema_four_archive` | ✅ |
| B-5 | Contract-2 fetch receipt subject is the typed row reference, not the body hash; fingerprints the whole completed attempt, body or not (plan §9.1, I4, I5) | `journal.py` receipt branch; `contracts.py` receipt key adds the contract | `::test_contract_two_fetch_receipt_rejects_body_hash_digest_and_fingerprints_rows`, `::test_contract_two_receipt_for_a_failed_attempt_without_body` | ✅ |
| B-6 | Contract-2 decision pins its discovery pass exactly (id, digest, collector hash); the research hash stays distinct; contract-1 parents forbidden (plan §9.2, J2, J3, N3) | `contracts.py` pin validation; `journal.py` decision branch | `::test_contract_two_decision_requires_the_exact_discovery_pin`, `::test_research_hash_rules_are_retained_under_contract_two`, `::test_contract_two_decision_cannot_cite_or_supersede_contract_one` | ✅ |
| B-7 | Mode table M4/M9/M10 for contract-2 receipts and decisions; reconstruction may cite live evidence but is never published (plan §9.3, K1–K3) | `journal.py:M9_DECISION_BINDING`, `M10_DECISION_DISCOVERY`, receipt M4 | `::test_mode_table_for_contract_two_receipts`, `::test_mode_table_for_contract_two_decisions` | ✅ |
| B-8 | One collector configuration hash per evaluation cohort; no pooling across hashes; no contract-1/contract-2 mixing (specification §5, plan §9.2, J4) | `journal.py:check_cohort` (in `append_evaluation` and `integrity`) | `::test_evaluation_cohort_has_exactly_one_collector_configuration_hash`, `::test_evaluation_cannot_mix_contract_one_and_contract_two_decisions`, `::test_research_hash_change_is_distinct_from_collector_provenance` | ✅ |
| B-9 | Unknown contract version in an archive fails integrity closed and names the row; dependent commands fail closed; bytes unchanged (plan §9.6, N5, N12) | `journal.py:integrity`, `ops/archive.py` | `::test_unknown_contract_version_in_an_archive_fails_closed_without_mutation`, `::test_contract_two_row_in_schema_three_archive_is_an_integrity_failure` | ✅ |
| B-10 | Migration 004 is transactional; backup/restore and replay preserve a mixed schema-4 archive byte for byte with per-contract counts (plan §9.6, N8, N10) | `schema.py:migrate` (one transaction), `ops/archive.py`, `replay.py` | `::test_backup_restore_and_replay_preserve_a_mixed_schema_four_archive`, `test_vertical_slice.py::test_replay_preserves_the_slice_and_reports_contract_counts` | ✅ |
| B-11 | Older binary (main at ebb58df) refuses a schema-4 archive at open, bytes unchanged (plan §9.6, N9) | version guards across `src/` and `ops/` | `::test_binary_at_main_refuses_a_schema_four_archive` (git archive of main, run with the real CLI) | ✅ |
| B-12 | Witness is a finite library function: writer lock, separate read-only connection, clock sampled after the read, one contract-2 receipt per subject, no caller wall time, injected clock → synthetic (plan §4.2, §4.3) | `forecast/witness.py` | `test_witness.py::test_one_receipt_per_subject_and_repeat_is_a_no_op`, `::test_budget_already_spent_on_entry_writes_nothing`, `::test_injected_clock_cannot_witness_a_live_pass` | ✅ |
| B-13 | Clock status from divergence, regression and negative-age checks; untrusted receipts written and permanent; restart resets only the monotonic baseline (plan §4.4, F1, F2, N13) | `witness.py:_high_water_mark`, divergence and negative-age logic | `::test_wall_clock_regression_between_runs_is_untrusted_and_unproven`, `::test_utc_monotonic_divergence_within_a_session_is_untrusted`, `::test_restart_after_divergence_resets_only_the_monotonic_baseline` | ✅ |
| B-14 | Subjects: passes newest-first ≤100; completed fetches in the scan window ≤500 newest-first inserted ascending; pending/interrupted reported never witnessed; caps and budget finish partial (plan §4.3, I1–I3) | `witness.py:_read_batch` | `::test_caps_and_budget_finish_partial_and_the_rest_is_picked_up_later`, `::test_pending_and_interrupted_attempts_are_reported_never_witnessed`, `::test_failed_attempt_without_body_is_witnessed_by_row_fingerprint` | ✅ |
| B-15 | Interrupted witness batch resumes at the later real time; original observed_at kept; witness never masks collector health (plan §4.2, §4.4, H4, E1, E2) | `witness.py` | `::test_interrupted_batch_resumes_at_the_later_real_time`, `::test_witness_after_failed_collection_leaves_health_unhealthy` | ✅ |
| B-16 | Read-only projection turns stored rows into the F3.1 policy arguments using only trusted contract-2 receipts observed by C; contract-1 receipts contribute nothing (plan §4.6, C1–C3) | `forecast/evidence.py:fetch_availability`, `discovery_as_of` | `test_evidence.py::test_receipt_after_cutoff_is_not_rescued_by_sequence_or_bytes`, `::test_contract_one_receipt_contributes_nothing_whatever_time_it_claims` | ✅ |
| B-17 | Applicable pass is furthest along in ingestion; orphan discovery fetch of a two-phase run → summary missing, no fallback; incomplete pass fails closed (plan §4.6, D1, F4, L2) | `evidence.py:discovery_as_of`, `verify_pass` | `::test_orphan_discovery_fetch_means_summary_missing_without_fallback`, `::test_orphans_only_from_two_phase_runs`, `::test_incomplete_latest_pass_fails_closed_without_falling_back`, `::test_unverifiable_complete_pass_is_never_a_usable_universe` | ✅ |
| B-18 | 300-second freshness preserved; stale pass not substituted; a pass first witnessed after C is not known at C (plan §4.6, §6, F3, F4) | `evidence.py:DiscoveryEvidence.eligibility_reasons` → `policy.freshness` | `::test_freshness_boundary_and_stale_pass_is_not_substituted`, `::test_pass_first_witnessed_after_cutoff_is_not_applicable` | ✅ |
| B-19 | Timing sweep: the §6 fifteen cells, discovery-duration sweep, and discovery columns identical as book outcome varies (plan §7, G1–G3) | projection, driven on a serial virtual timeline | `tests/forecast/test_timing_sweep.py` (21 cases) | ✅ |
| B-20 | Working vertical slice: collection → summary → witness → projection → policy eligibility → baselines (assignment §4) | all of the above | `tests/forecast/test_vertical_slice.py` | ✅ |
| B-21 | HTTP Date never an eligibility or clock-trust input (plan §4.1, §4.4, operator decision Q2) | witness and projection read no header; `Date` stays only in `fetches.headers_json` | no new code consults it; asserted by absence (see §13 note) | ✅ by construction |
| B-22 | Collector/research hashes distinct; each cohort one collector hash; changed collector hash → separate cohort; no pooling; 300 s threshold preserved (assignment §4) | `check_cohort`, the pin, `policy.freshness` | B-8, B-18 | ✅ |
| — | Decision/publication generation, selection service, evaluation materialization | deferred | plan §8.2 rows "Later"; specification F3.3b remainder, F3.4, F3.5 | ⬜ out of scope |

Routine choices and notes for Part B:

- **Witness namespace and modes.** Receipts are keyed by subject, digest, observer
  namespace, mode and contract version, so a contract-1 API receipt never blocks the
  witness. The projection reads one namespace/mode pair; a synthetic test archive is
  read with `mode="synthetic"`, a live archive with `forward_shadow`.
- **Orphan definition.** Only discovery-stage fetches (`status`, `series`, `document`,
  `markets`, `event`) of a two-phase run can be orphans; a Phase B `schedule` or `book`
  fetch of a run that already has a witnessed summary is not, so a book cannot fake an
  incomplete universe.
- **HTTP Date.** No new code path reads the `date` header. It remains captured in
  `fetches.headers_json` by the unchanged transport layer and is never an input to the
  witness, the projection or any policy function. This is the plan's Q2 decision.
- **What is deferred.** This branch does not generate decisions, publications or
  evaluation reports from live evidence; it provides the storage contracts, the witness
  that produces receipts, and the projection that feeds the pure F3.1 policy. Decision
  and evaluation generation are F3.3b-remainder, F3.4 and F3.5 with their own plans.
