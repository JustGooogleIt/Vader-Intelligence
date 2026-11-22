# F3.5 evaluation materialization

Branch `feat/f35-evaluation`, stacked on `feat/f34-completion` (PR #13), on
`fix/f33-foundation-repairs` (PR #12), on PR #11. Implemented 2026-10-14 on this Mac in
an isolated worktree (`uv sync --locked`, Python 3.11.6). Every archive is disposable and
every record synthetic. Nothing merged or deployed.

Governing documents: [specification](../specs/forecast-evaluation-v1.md) §5 (outcomes,
scores, views, coverage, corrections), §6 (`evaluation_runs`/`evaluation_items`, cohort
rule, transactions), §7 (`evaluate`, `evaluation inspect`, exit codes). Trace:
[release checklist](../specs/release-checklist.md) §2.

## Modules and interfaces

| Interface | What it does |
|---|---|
| `evaluation.outcomes.join(db, ticker, game_id, yes_team_id, outcomes_as_of, source_ceiling) -> Outcome` | Read-only join of one frozen opportunity to the outcome view bounded by retrieval time and fetch sequence. Latest completed settlement-market attempt by sequence, never the latest finalized result. |
| `evaluation.service.evaluate(store, protocol, view, outcomes_as_of, dataset, source_ceiling, start_date, through_date, collector_config_hash, budget) -> summary` | Freezes the population, derives publication status, joins outcomes, scores per view, writes one immutable run + items in one transaction. |
| `evaluation.service.inspect_evaluation`, `list_evaluations` | Read-only. |
| `evaluation.scoring.score_pair`, `mean`, `paired_summary` | Exact decimal strings; null for empty samples. |
| `vader evaluate --view … --outcomes-as-of … [--dataset --source-ceiling --from --through --collector-config-hash --budget]`, `vader evaluation inspect [--id N --items] [--limit --before --dataset --view]` | CLI; exit 0 complete, 3 inconclusive (no paired binary outcomes), 1 partial/failed. |

## Lifecycle, as implemented

| Situation | Result |
|---|---|
| Pending / unresolved outcome | Item `outcome_status=pending` (or `disputed` when the provider says so), unscored; evaluation `inconclusive` if nothing pairs. |
| Settled binary outcome | `binary_scorable`, `y` and exact payout string; scored according to the view. |
| Scalar / fractional payout, result–payout conflict | `exceptional_payout`, payout string retained, unscored. |
| Malformed or missing provider fields, non-binary market type | `invalid_or_missing_outcome`. |
| Latest attempt failed or did not parse | `outcome_fetch_failed`; an earlier good result is never reused. |
| No settlement attempt inside the boundary/ceiling | `outcome_not_observed`. |
| Mapping absent or not `mapped` | `identity_unresolved` (a label never scores without identity confirmation). |
| Mapping to another gamePk | `identity_changed`. |
| Decision abstained | `cutoff_status=ineligible`, unscored; publication `not_attempted` (`cutoff_ineligible`). |
| Binding with no decision in the dataset | `cutoff_status=unattempted`, counted in U; the report carries a labelled `cutoff_policy_annotation` computed read-only from the archive (never a decision or a forward probability). |
| Game with eligibility decisions but no binding | `cutoff_status=no_binding`, counted in U with its eligibility reason counts. |
| Candidate contract that resolved no game | Counted as `unresolved_candidates`, never a game opportunity. |
| Repeated materialization with identical manifests, boundary, view and versions | Returns the existing evaluation (`existing=true`); no row written. |
| Later or corrected outcome evidence for the same population | New evaluation with `supersedes_id` and `correction_reason` (`outcome_evidence_changed`, `publication_evidence_changed`, `code_changed`, `evaluator_changed`, `boundary_changed`, joined by `+`); the earlier evaluation and items are untouched. A→B→A retains three versions. |
| Budget exhausted before the report is frozen | Run `partial`, nothing written, population digest retained; retry recomputes deterministically. |
| Interruption or failure inside the report transaction | Rolled back; run `failed`; nothing half-written. |
| Evidence retrieved after `outcomes_as_of` or beyond the ceiling | Invisible to that evaluation; a later boundary supersedes. |

Publication status (forward view): `published_timely` (allowed record whose first
witness receipt is in `[C, C+150 s]` and before T with a trusted clock),
`late_publication`, `publication_unconfirmed` (allowed, no receipt), `vetoed`,
`missed`, `failed`, `not_attempted`. In the reconstruction view every item is
`not_applicable`, including a synthetic cohort simulating that view.

Scoring: forward view scores only `published_timely` items with `binary_scorable`
outcomes; reconstruction scores every `cutoff_eligible` item with a scorable outcome.
A midpoint abstention scores constant only. `paired_delta` is midpoint − constant.
Log-loss clipping is counted (`clipped`), never applied to the stored probability.

## Cohort isolation

An evaluation is one dataset namespace, one research protocol hash, one record mode
and one collector configuration hash. `evaluate` refuses a dataset whose decisions pin
several collector hashes unless `--collector-config-hash` selects one; the other
cohort's decisions are excluded, and a game decided anywhere in the dataset is never
also counted as unattempted. Unpinned abstentions (no applicable pass) are evaluated
only in a single-cohort dataset; with several cohorts they are listed as
`unattributed_decisions`. `journal.check_cohort` enforces the same rule at persistence
and in integrity (PR #12 F-4 made scores require the pin).

## Outcome evidence never enters selection

The forecast path reads fetches of stages `book`/`schedule`/`event`/… only; settlement
stages are never selected (`selection.select_inputs` filters `stage='book'`). Tested:
after a settlement refresh and an evaluation, `redecide` reproduces the stored result
and a reconstruction into a new dataset yields identical results with only the book as
a reference, even with the outcome inverted.

## What a report contains

`population` (U, E, K, M, P, FK, FM, FP, decisions, abstentions, unattempted,
no_binding, unresolved_candidates, scored counts); `coverage` ratios as
numerator/denominator/value (null on a zero denominator); `cutoff_status`,
`cutoff_primary_reason`, `publication_status`, `publication_reasons`, `outcome_status`,
`outcome_reasons`, `mlb_annotation`; `scores` (paired means and deltas, constant-only,
clipped count); identities (`dataset`, `protocol`, `research_config_hash`,
`collector_config_hash`, `code_hash`, `evaluator_version`, `runtime_version`, policy
versions, epsilon); `period` (dates, `outcomes_as_of`, ceiling, earliest/latest cutoff);
`evidence_mode` and `simulated_view`; `provenance_limitations`; per-opportunity
`annotations` (origin, probabilities, provider status/flags, outcome retrieval,
MLB agreement, cutoff-policy annotation); `collector_cohorts_in_dataset`;
`unattributed_decisions`; `cost` (`external_calls` 0, `local_compute` unmeasured).

## Decisions taken (flagged for review)

- A binary label requires a `mapped` settlement mapping agreeing with the frozen gamePk;
  without a mapping the item is `identity_unresolved`. Strict by design.
- Identity dispositions apply only when the outcome would otherwise be scorable; a
  pending market stays `pending` even if its mapping is missing.
- The evaluation `status` is `complete` when the paired scored N > 0, else
  `inconclusive`, matching the §7 exit-code rule.
- Supersession is keyed by identical `population_manifest` within (dataset, mode,
  protocol, config hash, view); a changed population is an independent evaluation.
- With several collector cohorts in one dataset, unpinned abstentions are reported, not
  pooled into every cohort.

## Tests

`tests/evaluation/test_materialization.py` (17): both views; vetoed publication as
coverage; pending → settled supersession without rewrite; A→B→A three versions;
exceptional/disputed/conflicting payouts visible and unscored; boundary hides later
evidence; MLB disagreement is an annotation; unattempted binding with annotation; game
without binding; mixed collector cohorts refused then evaluated separately; repeat
returns existing; budget exhaustion writes nothing; interruption leaves no half-report;
outcome evidence never enters selection; direct `outcomes.join` dispositions; CLI.
`tests/evaluation/test_scoring.py` gains `score_pair`/`mean`/`paired_summary` hand cases.

Full suite: 789 passed, 2 pre-existing `tests/worker_observer` failures. Ops 31 OK.
