# Independent review findings: status at the top of the stack

The integration reviewer (Grok, posted as PR reviews on #8–#27) numbers its findings
F1–F38, with N-notes and owner questions. This table records where each finding
stands at the top of the stack and where it was fixed. "Top" means #36 `6a2bc50`
before the repair PR #37. A finding the reviewer reproduced at a lower PR can already
be fixed higher up; the reviewer said it would check those when it reaches them.

Status values:
- **fixed (#N)**: fixed in that PR, verified at the top;
- **#37**: fixed in the repair PR #37;
- **owner**: needs an owner decision;
- **note**: documented, not changed.

## Inherited from #10–#16 (before the roadmap stack)

| ID | Sev | Where | Summary | Status |
|---|---|---|---|---|
| F1 | P1 | #11 | complete pass that marks the game not pregame still gave an eligible decision | fixed (#13) |
| F2 | P2 | #11 | a failed witness phase inside the tick reported `inconclusive` (exit 3), not failure | #37 |
| F3 | P2 | #12 | witness identity is self-declared; a receipt can predate its witness run | #37 |
| F4 | P2 | #12 | synthetic settlement fetches treated as live | fixed (#14) |
| F5 | P2 | #12 | a run with no lineage or protocol defaults to `live` | #37 |
| F6 / Q-M0-1 | P2 | #12 | contract 2 amended in place | fixed (#17, D-001; **accepted by the owner 2026-10-19**) |
| F7 | P3 | #8 | documented `pytest -q` fails collection (`ops` resolves to `tests/ops`) | #37 |
| F9 | P3 | #13 | `operational_veto` uses unwitnessed eligibility rows and their own time | #37 |
| F10 | P2 | #14 | outcome join takes labels from synthetic settlement runs | #37 |
| F11 | P2 | #14 | publication status trusts any `forecast-witness` receipt (F3 pattern) | #37 |
| F12 | P2 | #15 | research `holdout_history` lists only exact-range evaluations | #37 |
| F13 | P3 | #15 | a cohort with no decisions in a segment fails the research run | #37 |
| F14 | P3 | #15 | research-run lineage ignores the archive; docstring overstates idempotence | #37 |
| F15 | P3 | #16 | `shadow_health` misses two collection failure statuses | #37 |
| F16 | P3 | #16 | `shadow-evaluate` overlap report name collides | #37 |
| F17 | P3 | #17 | exact retry of a legacy receipt fails the current link check first | #37 |
| Q-M1-1 | — | #13 | later-tick publication path | owner |

## In the roadmap stack (#17–#27)

| ID | Sev | Where | Summary | Status |
|---|---|---|---|---|
| F18 | P2 | #18 | mapping policy 2 can map a market to a game moved from another date; one game backs two markets | #37 |
| F19 | P2 | #19 | an unparseable provider start aborts the whole evaluation | #37 |
| F20 | P3 | #20 | an unparseable `gameDate` fails the whole acquisition | #37 |
| F21 | P2 | #21 | holdout ledger keyed by split key allowed reuse under another key | fixed (#31, D-014: key is the pinned boundary) |
| F22 | P3 | #21 | same as F20 in the date-only path | #37 |
| F23 | P3 | #21 | text comparison of timestamps in collection datasets | #37 |
| F24 | P2 | #22 | `ArtifactError` was not a `ValueError`; one artifact stopped the tick | fixed (#33) |
| F25 | P2 | #22 | `vader evaluate --record-contract` cannot select contract 4 | #37 |
| F26 | P3 | #22 | NaN or out-of-range model output loses the decision or is clipped | #37 |
| F27 | P3 | #22 | text comparison of timestamps in `witnessed_listing` | #37 |
| F28 | P3 | #23 | symbolic artifact params not validated at construction | #37 |
| F29 | P3 | #23 | semantic dedup keeps the first, not the simplest, representative | #37 |
| F30 | P2 | #24/#25 | one `not_found` lookup finalizes an unknown submission; no open-orders sweep | #37 (narrowed in #25) |
| F31 | P3 | #24 | fill fee not checked against the reserved bound | fixed (#32) |
| F32 | P2 | #25 | the accepted-order guard lives only in process memory | #37 |
| F33 | P3 | #25 | `no_key_material` saw only PEM armour | fixed (#35) |
| F34 | P2 | #26 | holdout backtest simulated before recording; shares the model's holdout slot | ordering fixed (#31); one holdout use per model, either kind: owner |
| F35 | P3 | #26 | dry run does not filter by mode or supersession | #37 |
| F36 | P2 | #27 | pod results could register coordinator-only input kinds | fixed (#31) |
| F37 | P2 | #27 | an unrecognised baseline became a coin flip | fixed (#31) |
| F38 | P3 | #27 | the two holdout ledgers were not written together | fixed (#31, recovery from the registry entry) |
| F39 | P3 | — | listed as blocking in the #28 review, but not defined in any posted review | ask the reviewer |
| F40 | P2 | #28 | paper evidence not tied to an archive-built execution input or to conservative execution (assumed depth accepted); summary of another model accepted | #37 (another model's summary already refused since #31) |
| F41 | P3 | #28 | a shadow whose protocol lists forecasters validates without a loadable model registry | #37 |

## Notes

| ID | Summary | Status |
|---|---|---|
| N-20a | `interpret.candles` returns candles after `end_ts` | resolved at the dataset layer (reviewer) |
| N-20b | `--max-requests` counts logical requests, not HTTP attempts | #37 |
| N-20c | settled listing overrides the historical listing regardless of order | note; owner: the live record wins (deterministic, by stage order). Keeping the historical tier instead would switch 188–228 tickers of the real 2026 archive, which needs an interpretation version bump |
| N-21a | `cryptography` added before use | used from #25 on |
| N-24a | ledger accepts any `Approval` object | note: a test seam; the risk engine is the only producer in code |
| N-25a | `positions()` set state before the stray check | fixed (#32) |
| N-26a/b/c | dry-run replay label; extra `--assumed-depth` ignored; reused holdout reported as not selection-biased | #37 |
| N-28a | `tick_command` passes the registry path as stored, validation checks the resolved path | note: read-only registry, artifacts fetched by digest |
| N-28b | D-016 is a spec deviation (reviewer agrees) | repaired opt-in in #36 |
| N-28c | run documents not final at the reviewed head | final handoff after the 2026-10-20 evaluations |
| R64 (F34) | `simulate(segment="holdout")` consumes the holdout without acknowledgement from the library | #37 |

## After #37: integration repairs (#38) and release validation (2026-10-23)

Status values here: **#38** fixed in the consolidated repair `3b0ed7c`; **validated**
independently re-run on this branch (`docs/handoffs/claude-release-validation.md`).

| ID | Sev | Summary | Status |
|---|---|---|---|
| F39 | P3 | `RuntimeError` at summary commit left the run `running` | #38, validated; limitation: a failure record the disk also refuses still propagates |
| F47 | P3 | configure accepted artifacts the tick's runner refuses | #38, validated (the recovered wt-grok-h cases are ported as probes) |
| F48 | P2 | legacy init deposit re-added cash | #38, validated; Decimal comparison, ambiguous histories refused |
| F49 | P2 | a removed registry aborted the tick | #38, validated; corrupt/wrong-schema files also abstain, nothing is created |
| F50 | P2 | a refresh without a schedule turns a scored game `identity_unresolved` | **owner**: reconstructed; recovered `37c7bac` reviewed, mechanically sound, not applied; adopt only as `evaluation-v3` (proposed D-017) |
| F51 | P3 | headerless wrapped key bodies passed the paste guard | #38, validated; release validation narrowed a false positive (consecutive hex digests, absolute paths) |
| N-34a | P2 | an acknowledged settlement token hid a later venue revision | **fixed on `fix/claude-release-validation-20261022`**: acknowledgements cover only markets the ledger has not settled |
| N-37a/b/c/d | — | F30 lag residual; 1 ms run-start resolution; direct `ForecasterRunner` admits non-library artifacts (abstains); non-canonical split dates fail closed | notes, unchanged |

Owner decisions still open: #8 plan r3, Q-M1-1, D-002, D-003, D-004, D-005, D-015,
D-016 default, N-20c interpretation version, F34 (one holdout slot, either kind, is the
implemented rule), and F50. D-001 is accepted (owner comment on #17, 2026-10-19). The
reconciled table with recommendations is in `docs/handoffs/claude-release-validation.md`.
