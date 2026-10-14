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

## 1. Durable forecast path (F3.3/F3.4 completion)

| # | Requirement | Implementation | Tests | State |
|---|---|---|---|---|
| P-1 | Collection → discovery completion → lineage/receipts → witness → projection → selection → policy → decision/publication or abstention → inspect/replay through supported interfaces | (PRs #9, #10, #11 as repaired) | existing vertical slices | tested; M1 strengthens the assertions |
| P-2 | Identity, pregame and contract validity established from evidence at C, not hardcoded | pending | pending | pending |
| P-3 | Schedule/market renewal evidence at C from the applicable pass; start revision abstains | pending | pending | pending |
| P-4 | Book eligibility proof vs C start revision (`book_schedule_mismatch`) | pending | pending | pending |
| P-5 | Publication pre-commit operational check (veto with evidence) | pending | pending | pending |
| P-6 | Later tick publishes a decision lacking a record at the actual time | pending | pending | pending (Q-M1-1) |
| P-7 | `forecast reconstruct` archive-only campaign | pending | pending | pending |
| P-8 | Schedule-revision state machine (`decide_schedule`) | — | — | deferred (explicit abstention instead) |

## 2. F3.5 evaluation materialization — pending

## 3. Research runner — pending

## 4. Reports — pending

## 5. Forward-shadow workflow — pending

## 6. Hardening — pending
