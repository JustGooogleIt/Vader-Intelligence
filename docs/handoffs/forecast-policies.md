# F3.1 pure forecasting policies

Implemented only the user's F3.1 scope on `feat/forecast-policies`, from reviewed
code `9a34420947f5722109186ff4897f0b1f6cb18f09`. Both specification documents came
from `9e2ab907098fef763ebf7e4acbaacf8ebf8fa5b7`; their provisional review references
were reconciled. Publication vetoes no longer alter cutoff eligibility, 350-second
inputs remain stale, and independent discovery persistence is explicitly future work.
No remaining material contradiction affects these pure functions.

## Function contracts

All functions consume explicit inputs, perform no IO and read no clock. Frozen
result dataclasses contain immutable scalars/tuples. They do not establish archival
visibility, real game identity, live lineage, historical provenance or publication.

| Module / function | Contract |
|---|---|
| `forecast.baselines.constant()` | Unconditional Decimal `0.5`; call only after common eligibility passes. |
| `forecast.baselines.midpoint(book, notional="1.0000")` | One raw `orderbook_fp` payload. Reuses existing exact level validation and flags; returns YES Decimal midpoint or raises `InvalidBook` with `reason` and `flags`. Missing/null/empty/zero-size/crossed/flagged books never receive an imputed probability. No temporal qualification here. |
| `evaluation.scoring.probability(p)` | Exact nonnegative fixed-point string <=40 characters or equivalent finite Decimal; returns Decimal in [0,1]. Reject floats, ints/bools, signs, exponent strings and nonfinite values. Decimal representations are bounded before conversion (<=40 digits, exponent -38..0). |
| `evaluation.scoring.brier(p,y)` | Unclipped single-Bernoulli squared error; y must be integer 0 or 1, never bool, float or a payout. |
| `evaluation.scoring.clipped_probability(p)` / `log_loss(p,y)` | Fixed v1 epsilon 0.000001. Compare clipped p with original p to count clips. Natural-log loss returns Decimal and leaves original p unchanged. No averaging/cohort/evaluation engine. |
| `evaluation.settlement.finalized_binary(raw_market)` | Reuses the existing Kalshi interpreter on one raw market. Returns `SettlementEligibility(label,reasons)`; label is 0/1 only for unflagged, consistent finalized binary $1 settlement. Scalar 0/1/fractional payouts remain ineligible. Does not select a revision, join a game, or use MLB results. |
| `forecast.policy.decision_cutoff(T)` | Aware datetime normalized to UTC minus exactly one hour. Does not choose T from schedule history. |
| `forecast.policy.freshness(EvidenceTimes,C)` | Checks supplied retrieval and observed timestamps against C, inclusive age 0..300s. Missing visibility, post-C evidence or observation-before-retrieval fail closed. Returns `Eligibility(reasons)`; `eligible` is true exactly when reasons is empty. |
| `forecast.policy.cutoff_eligibility(...)` | Requires explicit boolean assertions plus discovery/schedule/market timestamps and binding observation time. Keeps deterministic provenance/clock, universe, identity, schedule/status reason order. Discovery uses phase completion as retrieval time; all source dependencies still require caller validation. No book, invocation, publication or settlement argument. |
| `forecast.policy.classify_publication(...)` | Classifies supplied operational facts separately. `attempted_at` is the final pre-commit check, `published_at` the post-commit witness. C and C+150s inclusive, before T. Missing witness is unconfirmed; late attempt missed; late witness late publication; known veto vetoed. A warning after the supplied check cannot retroactively revoke success. Reconstruction is not applicable and cannot relabel operational facts. `delay` is an exact timedelta for classified witnessed publications. |
| `forecast.selection.select_contract(candidates, team_ids)` | <=200 qualified candidate mappings for exactly one game. Reads only event_ticker/ticker/yes_team_id. ASCII lexical event, lower numeric team ID, lexical ticker; requires both teams. Does not fall back from a missing/ambiguous selected event. Returns selected frozen Contract or reasons plus deterministic alternatives. Extra prices/status/outcomes are ignored. Caller validates open/$1/current rules after selection. |

Numerics use a fresh Decimal context with precision 40 and ROUND_HALF_EVEN,
independent of the caller's precision/rounding/traps. Existing source prices allow
four decimals and quantities two. Midpoints are exact at up to five fractional
digits. Brier/log results use precision 40 without display rounding. Persist later
with `format(value, "f")`; do not convert to float. No console formatting or score
storage is implemented. Log replay tolerance remains the spec's 1e-30.

Malformed API values raise ValueError; valid-but-ineligible evidence returns reason
tuples (midpoint uses its documented InvalidBook exception). Reasons here are local
predicates, not a complete persisted rejection manifest. Examples/tests are synthetic.

## Verification

Windows, Python 3.12.14; private `.venv` with uv 0.12.19 and `uv sync --locked`.
No production shims, dependency changes or shared environment changes.

```powershell
.venv/Scripts/python.exe -m pytest --noconftest -q tests/forecast tests/evaluation
.venv/Scripts/ruff.exe check src tests ops
.venv/Scripts/ruff.exe format --check src tests ops
git diff --check
```

Focused tests: **152 passed**. Ruff lint/format and diff review passed.
`--noconftest` excludes the existing root test conftest, which imports Unix storage;
the new tests use only builtin pytest fixtures and no platform substitutions.
A fresh subprocess imports every new module and asserts storage, fcntl, sqlite3,
HTTP transport and CLI modules were not loaded. Tests cover exact scores and clipping,
hostile decimal contexts, settlement exclusions, freshness/publication microsecond
boundaries, the 350-second example and cycle-offset sweep, post-C veto separation,
all 120 orderings of a synthetic candidate set, and missing/ambiguous selection.
No full Unix integration-suite, native Mac, database or service claim is made.

## Later layers must guarantee

- Select the sticky schedule cutoff from witnessed history; prove identity, terms,
  pregame status, complete candidate membership and all transitive pre-C dependencies.
- Build the independent immutable discovery phase/receipt. Do not substitute current
  collector `discovery_complete` or a successful book pass for this future fact.
- Select the latest applicable attempted book, even when unusable; verify hashes,
  parser, same-contract/game eligibility and no intervening start revision. Do not
  filter candidate listings by prices/status/outcomes before deterministic selection.
- Verify supplied clock/receipt claims and synthetic lineage. Persist first publication
  outcome immutably; retries cannot replace a veto or inherit a fictional timestamp.
- Choose latest settlement attempts by the explicit as-of boundary, preserve revision
  identity/evidence, and join to the frozen contract/game. Never resurrect an older
  finalized result or use MLB as a binary label. Keep both evaluation views and paired
  coverage separate, with full population/exclusion manifests.
- Enforce uniqueness, schema/JSON-reference integrity, backup/replay and bounded IO
  when storage is introduced. None of those guarantees is supplied by pure predicates.

No schema 3, migration, receipt, journal, outcome join, CLI, collector change, scheduler
hook, full schedule-history state machine or evaluation engine was implemented.
The user narrowed F3.1 to the listed primitives; broader proposed interfaces in §7
remain future work. Prime's Mac checkout, configuration, archive and service are untouched.

Applied Cool Coder `implement`, `code-review`, `data-systems-design`, version 1.2.0
at `e46e79805be0ee5877fa9bc993492064bbb40aa5`, private installation read-only.
The user-specified F3.1 scope/spec overrides the historical plan/executor phases;
those shared files were not edited. Review gates: H-36/H-37 use supplied times and
fixed arithmetic/order; V-59/V-62 use bounded decimal/book/candidate validation;
no persistence, concurrency or distributed-system mechanism is introduced.

Next step: review this draft only. Stop after F3.1; no later phase or deployment is implied.

PR base check: at publication preparation, main was
`f0b0419ba45d1aeeb4ca663f6274981df09397d7`, which did not contain the reviewed
baseline. The draft therefore targets `feat/integration-v1` at `9a34420` so its diff
contains only F3.1 and the requested documents. Retarget to main after the integration
baseline is merged and ancestry is verified; do not merge as part of this task.
