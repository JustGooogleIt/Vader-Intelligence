# Forward-shadow operations and release hardening (milestones 5 and 6)

Branch `feat/forward-shadow-ops`, stacked on `feat/research-runner` (PR #15).
Implemented 2026-10-14 on this Mac in an isolated worktree (`uv sync --locked`, Python
3.11.6, uv 0.12.19). Nothing merged or deployed; no production archive, service,
registration or launchd job was touched; no credentials or paid APIs exist in this
system. Procedure and semantics: [docs/forward-shadow.md](../forward-shadow.md).
Trace: [release checklist](../specs/release-checklist.md) §5–§6.

## Actual forward-shadow evidence (public read-only smoke, isolated storage)

This is the only live evidence in the release. Everything else is synthetic.

| Item | Value |
|---|---|
| Storage | `…/scratchpad/shadow-smoke/shadow.sqlite3` (session scratchpad, outside every repository and production path); schema 4 created explicitly (`db-init`, `settlement migrate`, `forecast migrate`, `forecast migrate --record-contract 2`) |
| Configuration | `configure --test-label --shadow-research-config … --tick-budget 15 --forbid …/Vader-Intelligence/data --forbid …/Vader-Intelligence-claude/data`, cadence 120, deadline 90, grace 5; label `com.vader.test.collection.1a4aec7fe465460695e1`; launchd **not** installed (manual `run` cycles) |
| Duration | 2026-10-14T23:05:52Z to 23:14:20Z: **5 cycles, 8 min 28 s**, driver sleeping 120 s between cycles |
| Interruptions | none; no cycle timed out; no overlap; no background process left behind (driver exited) |
| Each cycle | tick (0.17–0.22 s, `inconclusive`, exit 3) then live two-phase collection (≈5.6–5.8 s, `discovery_no_eligible_games`, exit 3); wrapper exit 3 |
| Discovery | every pass complete, `forward_shadow`, 1 page, 10 candidate contracts in 5 events (postseason, e.g. `KXMLBGAME-26OCT142000MILLAD`, `…26OCT151800TBCLE`), official schedule window 2026-10-12..21 with 52 games of type `L`; **0 eligible**: `reason_counts = {"unrecognized_full_game_rule": 10}` |
| Witnessing | 48 contract-2 receipts, all `forward_shadow` and `trusted`, written by `forecast-witness` runs under live lineage (passes and fetches of the previous cycle); 0 untrusted |
| `health` after 4 cycles | collection `discovery_no_eligible_games`; `shadow.status = no_eligible_markets` ("discovery completed with no eligible contracts; inconclusive, not a failure"); three ≈5.7 s "collection gaps" recorded because the manual driver slept 120 s *after* each ≈6 s cycle (starts 126 s apart > cadence + grace); a launchd `StartInterval` would not produce them |
| `shadow-evaluate --from 2026-10-14 --through 2026-10-15 --settlement-limit 10 --settlement-budget 60` | settlement refresh `partial` in 11.5 s: 10 targets, 10 `kalshi` versions finalized (binary yes/no labels present), 10 mappings **quarantined** (`unrecognized_full_game_rule`), `selection_truncated` (more settled markets than the limit); research run `inconclusive` in 0.22 s: evaluation #1, U=0, `unresolved_candidates=10`; overall `partial` (after the label fix in this branch; the first run of the unfixed code printed `failed`) |
| Final `status` | `discovery_no_eligible_games`, `shadow.status=no_eligible_markets`, scheduler not loaded, 5 retained invocations, 4 recorded gaps (driver artefact), no storage pressure |

**Why no forecast was produced, exactly.** The live postseason contracts' rule text
reads `… professional baseball game originally scheduled for Oct 17, 2026, then the
market resolves to Yes.` with **no start time**, while the reviewed full-game rule
grammar (`scope.RULE`, also used by settlement `rule_identity`) requires `at H:MM AM/PM
EDT|EST`. The reviewed grammar therefore fails closed for every current contract, both
for eligibility (collector) and for settlement mapping (quarantined). This is the
correct conservative behaviour of the reviewed policy, not an operational failure, and
it was reported as `no_eligible_markets`. Changing the grammar is a reviewed
mapping/eligibility change with its own review: **decision needed (Q-M5-1)**: whether
to extend the reviewed rule grammar to time-less postseason rules. No league was
expanded and no synthetic evidence was relabelled.

A 24-hour gate was not run and is not claimed. The endurance evidence is exactly the
five cycles above.

## Hardening (milestone 6)

| Area | What was done | Where |
|---|---|---|
| Mutation before validation; unsupported combinations | `check_contracts` before WAL and migrations (PR #12); the wheel smoke asserts every schema/contract transition from the archive and a refusal without mutation | `tests/integration/wheel_smoke.py` (41 checks) |
| Lineage promotion; supplied assertions | PR #12; settlement lineage (PR #14) | `test_review_defects.py` |
| Future information through joins/caches/replay | projection admits receipts ≤ C only; selection never reads settlement; `discovery_as_of` made scalar-first | `test_materialization.py::test_outcome_evidence_never_enters_forecast_selection`, perf probe |
| Cross-cohort scoring | one collector hash per evaluation | `test_materialization.py` |
| Publication/cutoff ordering | `test_hardening.py::test_publication_never_precedes_its_cutoff` | |
| Duplicate logical work under concurrency | writer lock + content keys; concurrent ticks and evaluations | `test_tick.py`, `test_hardening.py` |
| Partial writes, crash recovery | single transactions; interrupted reconstruction resumes | `test_hardening.py`, `test_materialization.py` |
| Legacy data blocking valid work | witness skips legacy subjects; contract-1 rows beside new work | `test_hardening.py::test_legacy_records_never_block_unrelated_valid_work` |
| Limits bounding work | 1000-opportunity/1000-game refusals; per-invocation witness caps | `test_hardening.py`, `test_review_defects.py` |
| Backup/restore interpretation | report, integrity and evaluation identity equal after two backup hops | `test_hardening.py::test_backup_and_restore_preserve_interpretation_and_provenance` |
| Property-based / state-machine tests | not added: `hypothesis` is not in `uv.lock`; deterministic fault injection used instead | — |

## Performance (synthetic archive, this Mac, `tests/integration/perf_probe.py`)

Rows at 100 games × 200 passes: 1,000 fetches, 200 passes, 1,200 receipts, 100
bindings, 100 decisions, 40,000 eligibility rows; archive 22.8 MB.

| Operation | 50 games × 20 passes | 100 games × 200 passes (before) | 100 × 200 (after fix) |
|---|---|---|---|
| `check_contracts` (every Store open) | 0.7 ms | 7.8 ms | 11 ms |
| `discovery_as_of` (per cutoff) | 6.5 ms, 1.0 MiB | 67 ms, **10.9 MiB** | 44 ms, **0.24 MiB** |
| `select_inputs` | 0.2 ms | 0.4 ms | 0.4 ms |
| `journal.integrity` (replay/inspect/backup) | 0.16 s | 1.61 s | 1.86 s |
| `inspect` page of 20 | 0.7 ms | 0.8 ms | — |
| `evaluate` forward view (100 decisions) | 0.09 s | 0.35 s | 0.38 s |
| read-only Store open | 1.8 ms | 9.2 ms | — |

Finding and fix: `evidence.discovery_as_of` loaded and parsed every witnessed pass
manifest for every cutoff; memory and time grew with the whole history (720 passes/day).
It now reads scalars through SQL and loads only the applicable pass. Remaining known
linear cost: `journal.integrity` re-validates every typed reference of every pass and
receipt (plan risk R3), ≈8–9 ms per pass here; it runs in replay, inspect, backup and
restore, never in the tick. Timing noise between the two 100×200 runs is ±15%.

## Baseline failures reconfirmed

`tests/worker_observer/test_observer.py::test_upstream_pin_guard_and_cli_help` and
`::test_bridge_assessment_uses_frozen_input_with_mock_provider` fail identically on
every branch of this release and on the PR #11 base (`ModuleNotFoundError: tmux_jev`,
an optional external tool absent in the locked environment). No unrelated code was
changed to alter that count.

## Tests at the branch head

Full suite: 816 passed, 2 pre-existing failures; ops suite 42 OK; ruff check/format
clean; `git diff --check` clean; installed-wheel smoke 41 checks from an environment
outside the checkout (`…/scratchpad/venv-wheel`), module path in `site-packages`.
