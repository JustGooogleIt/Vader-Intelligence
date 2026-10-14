# Research-pipeline release checklist: requirement → implementation → tests → PR → gaps

Living matrix for the release that completes the research pipeline (foundation repairs,
F3.4 completion, F3.5 evaluation, research runner and reports, isolated forward
shadow, hardening). States: **implemented**, **tested**, **demonstrated** (run on
disposable data with recorded output), **deferred** (named later increment),
**blocked** (names the missing decision or dependency).

Governing documents: `docs/specs/forecast-evaluation-v1.md` (spec), `docs/specs/f33-implementation-plan.md`
(plan), the F3.3/F3.4 checklists, and the independent PR #10 review (six defects).

## 0. Foundation repairs (PR: `fix/f33-foundation-repairs`, stacked on #11)

| # | Requirement | Implementation | Tests | State |
|---|---|---|---|---|
| F-1 | Synthetic collection never becomes forward evidence (review 1, plan §9.3 M1/M2) | `collector.py` records `source_lineage`; `references.run_source_mode`; `witness._read_batch` lineage; `journal.validate_links` lineage rule; `evidence.verify_pass` `pass_lineage_mismatch` | `test_review_defects.py::test_synthetic_collector_fetches_never_become_forward_evidence`, `::test_resume_cannot_change_a_run_source_lineage` | tested |
| F-2 | Supplied assertions are never operational proof (review 2, plan §4.1) | `journal.validate_links` witness-run rule; `evidence._receipt_filter`, `witness.WITNESS_RECEIPT`, `tick._bindable_games` join on witness runs | `::test_supplied_receipts_are_rejected_and_never_operational_proof`, `::test_projection_ignores_a_supplied_receipt_present_in_a_malformed_archive`, `test_record_contract.py::test_mode_table_for_contract_two_receipts` | tested |
| F-3 | Invalid contract archives rejected before mutation; migration never legitimizes (review 3, plan §9.6) | `schema.check_contracts`; `Store._validated_schema_version` and read-only open; `schema.migrate`; `cli.py` preflight | `::test_invalid_contract_rejected_without_mutation` (10 cases), `::test_rejected_schema2_contract_migration_leaves_delete_mode`, `::test_004_failure_rolls_back_then_retry_preserves_rows`, `test_record_contract.py::test_contract_two_row_in_schema_three_archive_is_an_integrity_failure` | tested |
| F-4 | Scored unpinned decisions cannot bypass cohort isolation (review 4, spec §5) | `journal.validate_links` evaluation-item score rules; `journal.check_cohort` | `::test_scored_unpinned_rows_cannot_bypass_cohort_check` | tested |
| F-5 | Legacy reconstruction records never obstruct witnessing (review 5) | `witness._read_batch` skips H passes/bindings; per-subject refusal is reported and skipped | `::test_legacy_reconstruction_pass_does_not_poison_witness` | tested |
| F-6 | Witness limits per invocation (review 6, plan §4.3) | `witness.witness` cumulative allowances, `remaining_by_kind` | `::test_witness_pass_limit_is_per_invocation`, `::test_witness_fetch_limit_is_per_invocation` | tested |
| F-7 | Existing suites unchanged in meaning | — | full suite 749 passed; 2 pre-existing `tmux_jev` failures | tested |

## 1. Durable forecast path (PR: `feat/f34-completion`, stacked on `fix/f33-foundation-repairs`)

| # | Requirement | Implementation | Tests | State |
|---|---|---|---|---|
| P-1 | Collection → discovery completion → lineage/receipts → witness → projection → selection → policy → decision/publication or abstention → inspect/replay through supported interfaces | PRs #9/#10/#11 as repaired; `forecast/tick.py`, `cli.py` | `test_vertical_slice_f34.py`, `test_tick.py`, `test_f34_completion.py` | tested |
| P-2 | Identity, pregame and contract validity established from evidence at C, not asserted | `service.renewal_at_cutoff`, `decide_opportunity` | `test_f34_completion.py::test_renewal_uses_the_applicable_pass_at_cutoff_not_the_binding_evidence`, `::test_latest_pre_cutoff_evidence_invalidates_eligibility_with_its_reason` (5 cases), `::test_game_missing_from_the_applicable_schedule_is_not_permission_to_reuse` | tested |
| P-3 | Schedule/market renewal evidence at C from the applicable pass; a binding not renewed abstains; start revision abstains | same | `::test_binding_not_renewed_by_cutoff_abstains_on_stale_evidence`, revision case above | tested |
| P-4 | Book eligibility proof vs the binding's start (`book_schedule_mismatch`) | `selection.select_inputs(scheduled_start=…)` | `::test_book_collected_under_another_schedule_version_is_rejected` | tested |
| P-5 | Publication pre-commit check of latest available post-C evidence; veto with evidence; cutoff facts intact | `tick.operational_veto`, `tick._publish` | `::test_post_cutoff_postponement_vetoes_publication_but_not_the_decision`, `::test_post_cutoff_evidence_that_keeps_the_game_pregame_does_not_veto` | tested |
| P-6 | Later tick publishes a decision lacking a record at the actual time; missed after the window | `tick._unpublished_decisions`, Phase 5 | `test_tick.py::test_crash_between_decision_and_publication_is_published_later_at_actual_time`, `::test_decision_left_unpublished_past_the_window_is_recorded_missed` | tested (decision Q-M1-1) |
| P-7 | `forecast reconstruct` archive-only campaign: explicit namespace, 31-day/1000-game bounds, source ceiling, H for forward bindings / S for synthetic, no publication, idempotent | `forecast/reconstruct.py`, `cli.py` | `::test_reconstruct_recomputes_the_cutoff_decision_without_publication`, `::test_reconstruct_refuses_the_forward_namespace_and_unbounded_ranges`, `::test_reconstruct_source_ceiling_cannot_exclude_pre_cutoff_evidence`, `::test_reconstruct_through_the_cli` | tested |
| P-8 | Inspect explains renewal, reasons, publication and its witnessed receipt; dataset filter | `tick.inspect` | `::test_inspect_explains_renewal_and_publication` | tested |
| P-9 | Abstentions preserve input IDs (binding, pass pin, ticker) | `decide_opportunity` | `test_service.py::test_decide_abstains_when_binding_not_witnessed_by_cutoff` | tested |
| P-10 | Schedule-revision state machine (`decide_schedule`, moving C, binding chains) | — | — | deferred: explicit `schedule_revised` abstention instead; first reached C stays sticky |

## 2. F3.5 evaluation materialization (PR: `feat/f35-evaluation`, stacked on `feat/f34-completion`)

| # | Requirement | Implementation | Tests | State |
|---|---|---|---|---|
| E-1 | Join persisted decisions/publications to valid outcome evidence at an explicit `outcomes_as_of` boundary and source ceiling; latest completed attempt, never latest finalized (spec §5) | `evaluation/outcomes.py:join` | `test_materialization.py::test_outcome_join_dispositions_directly`, `::test_later_evidence_after_the_boundary_is_invisible` | tested |
| E-2 | Pending/unresolved, settled, canceled/voided/exceptional, conflicting evidence, missing predictions/abstentions handled explicitly | `outcomes.join` dispositions; `service.evaluate` population | `::test_pending_then_settled_outcome_supersedes_without_rewriting`, `::test_ineligible_outcomes_are_visible_and_unscored` (3), `::test_unattempted_binding_is_counted_with_a_labelled_annotation`, `::test_game_without_binding_is_counted_from_the_frozen_manifest` | tested |
| E-3 | Repeated materialization returns the existing evaluation; interrupted materialization and budget exhaustion write nothing; restart recomputes | `service.evaluate` key lookup, single `append_evaluation` transaction | `::test_repeat_with_identical_manifests_returns_the_existing_evaluation`, `::test_budget_exhaustion_writes_nothing_and_retry_succeeds`, `::test_interrupted_materialization_leaves_no_half_report` | tested |
| E-4 | Later evidence appends a superseding evaluation; originals immutable; A→B→A keeps three versions | `service.evaluate` supersedes logic; journal triggers | `::test_pending_then_settled_outcome_supersedes_without_rewriting`, `::test_a_b_a_corrections_retain_three_evaluations` | tested |
| E-5 | Collector-hash cohort isolation at materialization and reporting, including alternate API paths | `service.evaluate` cohort selection; `journal.check_cohort` (PR #12) | `::test_mixed_collector_cohorts_are_evaluated_separately`, `test_review_defects.py::test_scored_unpinned_rows_cannot_bypass_cohort_check` | tested |
| E-6 | Outcome evidence never enters historical selection or replay | selection reads book stages only | `::test_outcome_evidence_never_enters_forecast_selection` | tested |
| E-7 | Spec metrics (Brier, natural-log loss with 1e-6 clipping, paired deltas, means) tested independently with hand cases, boundaries, invalid inputs; no silent clipping of stored p | `evaluation/scoring.py` | `test_scoring.py` (F3.1 cases + `score_pair`/`mean`/`paired_summary`) | tested |
| E-8 | Two views never pooled; forward scores only `published_timely`; reconstruction scores the cutoff population | `service.evaluate` | `::test_forward_view_scores_only_timely_published_decisions`, `::test_reconstruction_view_scores_cutoff_population_without_publication`, `::test_vetoed_publication_is_coverage_not_score_in_forward_view` | tested |
| E-9 | MLB result is an annotation, never a label | `outcomes.join` | `::test_mlb_disagreement_is_an_annotation_not_a_label` | tested |
| E-10 | CLI `evaluate` / `evaluation inspect` with spec exit codes | `cli.py` | `::test_evaluate_and_inspect_through_the_cli` | tested |
| E-11 | Evaluation of a population above 1000 opportunities is an explicit error, never truncation | `service.MAX_OPPORTUNITIES` | not exercised (needs >1000 games) | code only |


## 3. Research runner (PR: `feat/research-runner`, stacked on `feat/f35-evaluation`)

| # | Requirement | Implementation | Tests | State |
|---|---|---|---|---|
| R-1 | Accept archive, protocol/configuration and evaluation scope; immutable run identity; code/configuration provenance via existing conventions | `research/runner.py:run` (`runs` row kind `research-run`, `archive_identity`) | `test_runner_and_reports.py::test_forward_run_evaluates_each_cohort_once_and_resumes_without_duplicates` | tested |
| R-2 | Observations processed in temporal order | reconstruct in cutoff order; development before holdout | `::test_reconstruction_run_reconstructs_then_evaluates_in_its_own_namespace`, `::test_split_evaluates_development_before_holdout_and_records_history` | tested |
| R-3 | Exact inputs referenced for reproduction | decision manifests, evaluation manifests, archive identity, protocol hash | same | tested |
| R-4 | Explicit completion, partial and failure states; resume without duplicate logical results | run statuses; content keys | `::test_budget_exhaustion_is_partial_and_writes_no_evaluation`, resume case in R-1 | tested |
| R-5 | Synthetic, reconstruction and forward results separate; incompatible cohorts never pooled | per-cohort evaluations; evaluation mode | `::test_reconstruction_run_reconstructs_then_evaluates_in_its_own_namespace`, `test_materialization.py::test_mixed_collector_cohorts_are_evaluated_separately` | tested |
| R-6 | Explicit split boundaries; held-out observations not fitted; provenance to audit | split validation, `holdout_history` | `::test_split_evaluates_development_before_holdout_and_records_history` | tested (procedural; v1 has no tunable parameter) |
| R-7 | Supported baselines and policies | constant-v1 and midpoint-v1 (the fixed pair) | all | tested |
| R-8 | No LLM providers, training, symbolic regression or agent competition | — | — | not added by design |

## 4. Reports (same PR)

| # | Requirement | Implementation | Tests | State |
|---|---|---|---|---|
| T-1 | Machine-readable and concise human-readable reports via existing CLI | `research/report.py`, `vader evaluation report` | `::test_text_and_machine_reports`, `::test_research_run_and_reports_through_the_cli` | tested |
| T-2 | Metrics with eligible population and scored N; coverage and abstention counts by reason; pending/excluded/invalid/unresolved outcomes; collector and research identities; policy identity; evidence mode and limitations; period | evaluation report fields | same; `test_materialization.py` | tested |
| T-3 | Policy comparison on the same observations; coverage differences exposed | paired constant/midpoint within an evaluation; `compare` restricted to common scored opportunities | `::test_compare_restricts_to_common_scored_opportunities` | tested |
| T-4 | Cohorts separate by default; higher-level summaries labelled | per-cohort evaluations; compare labels different cohorts, refuses different views/modes | same | tested |
| T-5 | No synthetic-as-edge, no PnL, no profitability claim | `provenance_limitations`, report text | demo output | tested |
| T-6 | Small deterministic demonstration with expected results | `tests/integration/research_demo.py` | `tests/research/test_demo.py` | demonstrated |


## 5. Forward-shadow workflow — pending

## 6. Hardening — pending
