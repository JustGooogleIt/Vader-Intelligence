# F3.4 completion: evidence at the cutoff, vetoes, late publication, reconstruct

Branch `feat/f34-completion`, stacked on `fix/f33-foundation-repairs` (PR #12), on PR
#11 `5811991`. Implemented 2026-10-14 on this Mac in an isolated worktree with its own
`uv sync --locked` environment (Python 3.11.6, uv 0.12.19). Every archive is disposable
and every record synthetic (injected transport and clock). Nothing merged or deployed.

Governing documents: [specification](../specs/forecast-evaluation-v1.md) §2 (three
clocks, schedule rules 4–6, three independent decisions), §3 (population and selection),
§4 (abstention reasons), §6 (persistence), §7 (tick, reconstruct, exit codes). Trace:
[release checklist](../specs/release-checklist.md) §1.

## What was wrong and what changed

The PR #11 decision path asserted `identity_proven`, `pregame` and `contract_valid`
from the *presence* of a binding and a complete pass, and checked schedule/market
freshness against the binding's original fetches. In real operation a binding is
prepared when the game is first discovered, hours before C, so every forward decision
would have abstained on stale evidence, and the slice only passed because its binding
was prepared two minutes before C.

**Renewal at C** (`forecast/service.py:renewal_at_cutoff`). The applicable discovery
pass at C (most recent complete pass witnessed by C, from the unchanged projection) must
still list the selected contract under the same event, the official schedule in that
pass must still contain the game, and that pass's eligibility decision for the selected
contract establishes the three predicates:

| Predicate | Established from | Failure detail (appended after the policy reason) |
|---|---|---|
| identity | row eligible with the binding's gamePk and YES team; same event listing | `identity_changed`, `event_listing_changed`, `contract_missing_from_discovery`, or a `scope.eligible` identity reason |
| pregame | row eligible and its `scheduled_start` equals the binding's start | `schedule_revised`, `schedule_missing_game`, or a schedule/status reason (`not_scheduled_pregame`, `rescheduled_game`, `schedule_rule_time_mismatch`, `start_time_unknown`, …) |
| contract validity | row eligible | `market_not_open`, `unsupported_notional`, `unrecognized_full_game_rule`, … |

The schedule and event fetches behind that row are the renewal inputs: both must be
retrieved and witnessed by C within 300 s (`policy.freshness`, unchanged). The decision
`reasons` list is the pure policy's fixed-order reasons followed by these details, so the
primary reason is still the first applicable stage. An unknown eligibility reason fails
every predicate closed. The start-revision state machine (moving C) remains deferred:
a start that changed after the binding abstains explicitly with `schedule_revised`.

**Abstentions keep provenance.** A decision now always records the binding it
considered, the selected ticker/YES team/start, and the applicable pass (pinned) even
when it abstains. Only `cutoff_eligible` and the probabilities differ.

**Book schedule version** (`selection.select_inputs(scheduled_start=…)`): the book's
own eligibility proof must carry the binding's start, otherwise `book_schedule_mismatch`.

**Publication** (`forecast/tick.py`):

- Phase 5 attempts publication for every eligible decision of the cohort that has no
  publication record, at the tick's actual time. A decision whose publication never
  committed (crash between the two records) is published by a later tick at that later
  time: `allowed` inside `[C, C+150 s]`, `missed` (`late_invocation`) after it. Never
  backdated; the publication records the decision's own `created_at` separately.
- Immediately before committing, `operational_veto` reads the newest committed
  eligibility decision for the contract made after the one the cutoff decision renewed
  on. If it shows the game not pregame or the market no longer open, the publication is
  `vetoed` with that reason, its observation time and typed schedule/event references.
  The cutoff decision is untouched. No request is made.
- A retry returns the original record (one publication per decision).

**Reconstruct** (`forecast/reconstruct.py`, `vader forecast reconstruct`): explicit
`--dataset` (must differ from the forward namespace), `--from/--through` original dates
(1..31 days), optional `--source-ceiling` (recorded in every decision; refused if it
would exclude evidence retrieved before a cutoff in range), `--forward-dataset` to name
the forward cohort whose bindings are reconstructed. Forward bindings yield
`historical_reconstruction` decisions citing the live binding and pass (M9/M10);
synthetic bindings yield `synthetic` decisions. No publication, no HTTP, at most 1000
games (explicit error, never truncation), idempotent and resumable.

## Decisions taken (flagged for review)

- **Q-M1-1 later publication**: a later tick publishes an eligible decision that lacks
  a publication record, at its actual time. Alternative considered: leave it
  `not_attempted` forever. The spec's "attempt operational publication separately;
  record missed opportunities" and "no attempt is not_attempted" are read as describing
  the ledger state while no record exists, not as forbidding a later actual attempt.
- Abstentions retain binding/pass provenance (input IDs preserved for rejected inputs).
- `Protocol.config_hash` still includes the dataset namespace (PR #11 behaviour kept),
  so reconstruct must name the forward dataset to find its bindings.

## Tests

`tests/forecast/test_f34_completion.py` (17): renewal from the applicable pass rather
than the binding's fetches; stale abstention without renewal; start revision,
postponement, reschedule flag, closed market, changed notional; game missing from the
schedule; book schedule mismatch; post-cutoff postponement vetoes publication but not
the decision; same evidence does not veto; inspect explains renewal and the witnessed
publication; reconstruct recompute/idempotence/namespace/range/ceiling/CLI; redecide.
`tests/forecast/test_tick.py` gains the late-publication cases.

Full suite at `511f3c6`: 767 passed, 2 pre-existing `tests/worker_observer` failures
(missing optional `tmux_jev`). Ops suite 31 OK. ruff clean.
