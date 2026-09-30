# Milestone 3: baseline forecasts and evaluation v1

Status: implementation specification, **not implemented**. Written 2026-09-30
against integration commit `9d3d1ea2f167d5154dd6c67b3f47f2c7640fced7`.
PR #4 is independently under review. All proposed shared-interface changes below
are provisional until that review is reconciled. This document and the
[handoff](../handoffs/forecast-evaluation-spec.md) are the requested F3 planning
artifacts; historical `plan.md` and `executor.md` remain unchanged.

## 1. Scope and measured starting point

Produce reproducible T−60 forecasts for MLB `KXMLBGAME` full-game $1 team-winner
contracts, then score their YES probabilities against official Kalshi finalized
binary settlements. The two references are `constant-v1` (0.5) and
`midpoint-v1` (one contemporaneous two-sided book). Neither is a trading strategy.
Keep MLB results, coverage, runtime and costs separate. No forecast agents,
symbolic regression, Kelly sizing, trading, fill simulation, P&L, dashboard,
new provider calls or orchestration framework.

The integration evidence reports two actual collection passes of eight books/four
games in about 9.6 and 9.1 seconds, under a 120-second cadence and 90-second wrapper
deadline. This is not a tail-latency guarantee or continuous-operation evidence.
The current implementation has schema 1 collection and explicit schema 2 settlement;
it has no forecast ledger, decision scheduler or evaluation implementation.

Grounding at the pinned commit (paths relative to repository root):

| Existing interface | Reuse and gap |
|---|---|
| `migrations/001_initial.sql`: `fetches`, `blobs`, `entities`, `observations`, `eligibility`, `runs` | Raw IDs/bytes, retrieval times, sequence and parser versions exist. `retrieved_at` is HTTP completion, **not database commit time**. Sequence is allocated at request start. Normalized row IDs may be created later by replay. |
| `normalize.py:book_data`, `normalizer` | Decimal prices; YES ask complements the best NO bid; complete levels and side states retained. Books reference `eligibility_id`. Missing/empty and crossed books are distinct. |
| `scope.py:eligible`, `collector.py:record_eligibility` | Pregame identity evidence references schedule and event/market fetches. `mapping_version=1` is an algorithm label, not an immutable mapping revision. `scope.eligible` rejects reschedules and requires rule start equal to schedule start. |
| `mapping/__init__.py:rule_identity`, `resolve` | Reviewed aliases; game-number handling; explicit prior identity/reschedule links. `resolve` accepts non-pregame MLB states for settlement: it is **not** a forecast eligibility predicate. |
| `settlement/journal.py:record`, `map_target`, `latest` | Append-only semantic versions, observation links and complete target evidence tuples. `latest`/`before_seq` is not a decision-time query: anchor sequence alone does not prove every supporting input was available then. |
| `settlement/models.py:kalshi_result`, `mlb_result` | Independent outcomes. Finalized binary labels require matching exact payout and valid timestamp, with no flags. `is_provisional` does not mean disputed settlement in the verified interpretation. |
| `storage.py:Store.transaction`, `writer_lock`, `health` | `BEGIN IMMEDIATE`, WAL/FULL, canonical local writer lock; health excludes settlement but would currently consider a new forecast run kind. Must fix before introducing those run kinds. |
| `replay.py:replay`, `import_fixture` | Idempotent normalizer replay; fixtures have `runs.kind='fixture'`. Replay cannot establish historical publication time. |
| `provenance.py:RunProvenanceV1`, `source_hash`, `json_text` | Existing slots for forecasts, inputs, outcomes, evaluator, runtime/cost. Code hash excludes `ops/`; include separate operations artifact identity if later scheduled. |
| `cli.py`, `settlement/cli.py`, `config.py` | Follow subcommand registration; current TOML parser accepts only `[collector]`. Add a separate research-config argument/parser, not unknown fields in that parser. |
| `ops/archive.py`, `ops/runner.py`, `ops/common.py` | Explicit schemas 1/2 only; collection classification requires `kind='collect'`. Runner executes only the collector. No implicit forecast hook exists. |

Read the tests as constraints: `tests/test_scope_and_prices.py` covers decimal,
empty/crossed, identity and cutoff cases; `tests/settlement/test_settlement.py`
covers corrections A→B→A, timestamp flags, exceptional payouts, mapping revisions
and ordered replay; `tests/test_integration.py` covers schemas, cutoff crossings,
health, locks and restored archives. F3 must retain those behaviors.

## 2. Decision clock and availability contract

### Defaults (one versioned protocol, not tunable during a cohort)

| Setting | v1 default and meaning |
|---|---|
| `protocol_id` | `mlb-t60-v1`; canonical configuration hash also required |
| `horizon_seconds` | 3600, the only v1 horizon |
| `book_max_age_seconds` | 300, inclusive at cutoff |
| `schedule_max_age_seconds` | 300, inclusive at cutoff; separate from the collector's existing stricter 120-second request guard |
| `market_max_age_seconds` | 300 for event/market status and current rules evidence |
| `record_grace_seconds` | 150 after cutoff; labels record delay explicitly |
| `log_epsilon` | Exact string `0.000001`; evaluation configuration, not a probability edit |
| `max_games` / `max_contracts` | 100 / 200 per invocation; exceed either => explicit incomplete manifest, never truncate silently |
| `budget_seconds` | 15 for one offline tick; 30 for evaluation, including database work |

300 seconds permits two nominal 120-second cycles plus 60 seconds of slippage.
It tolerates a single missed cycle, not an outage. A 120-second threshold would
reject otherwise expected books when game requests shift within successive passes;
600 seconds would admit five-cycle-old information without measured justification.
The choice is a research policy, not proof that a five-minute-old market is current.
Persist ages and report their distribution. Changing freshness or grace creates
a new protocol/configuration cohort; do not optimize thresholds on reported results.
Accept configurable positive freshness up to 600 seconds and grace up to 180 seconds
for explicitly different protocols; reject zero, negative, nonfinite or unknown settings.

### Three clocks, plus evidence of visibility

- `scheduled_start = T`: official MLB schedule timestamp in a particular version.
- `decision_cutoff = C = T − 3600 seconds`: the information cutoff, never Kalshi close
  or expiration time. All timestamps are aware UTC; retain original provider strings.
- `created_at`: actual local insertion time of the forecast record; never set to C
  during backfill. Durations/deadlines use monotonic time. Sequence orders ingestion;
  provider `updated_time` never determines when the local system knew something.

An input needs both `retrieved_at <= C` and proof that its completed raw bytes were
visible to the local reader by C. Do not infer the second from `fetches.seq`, an
entity ID, a provider timestamp or a raw-body hash. A request started before C and
completed after C is ineligible, even if its source timestamp predates C.

Propose a small append-only **visibility receipt** table (§6). After a read transaction
has read a previously committed fetch/binding/decision or completed discovery-run
summary, sample real UTC and monotonic
time and append a receipt with that observed time and subject digest. That time is
a conservative upper bound on availability, not an invented exact commit timestamp.
The read and observation precede receipt insertion. Never write a pre-commit timestamp
and call it committed. No user-supplied wall time is allowed in live commands.
Receipts use a local session ID and clock checks, and cannot be imported as live proof.

`forecast tick` witnesses completed retrievals, prepares bindings, then witnesses
the committed bindings. Future ticks freeze due decisions using only receipts whose
observation times are <= C. This is all local: no network call inside any transaction.
Receipt insertion can happen after C only when the actual observation occurred before
C within that same uninterrupted live invocation; never reconstruct that observation
time from HTTP metadata. A crash before saving the receipt loses proof, not evidence;
later receipt timestamps are actual later times and may cause abstention.

For legacy archives without receipts, historical reconstruction may use a **documented
conservative witness**: a completed, non-resumed run was observed finished before C in
an existing immutable observation record. `runs.finished_at` alone is mutable and
sampled before its transaction commits, so is insufficient. Otherwise mark
`availability_unproven`; a receipt obtained today cannot prove yesterday's availability.
Raw pre-cutoff retrievals may still be inspected as a diagnostic reconstruction but
must not enter the strict time-valid score cohort. This intentionally limits legacy
backtesting rather than silently strengthening archive guarantees.

### Schedule version selection (no retroactive T−60)

Maintain a prepared binding chain for each known game, using only chronologically
observed evidence. Iterate receipts by observed UTC, then receipt ID for ties, and
reject clock regressions. As of a time, use the latest supported official schedule
observation, not the latest eventual schedule. If duplicate game entries conflict,
quarantine; do not pick the most convenient start. The binding includes the raw
schedule retrieval and the game entry digest. A missing row in a later complete
schedule for the same covered date is `schedule_missing_game`, not permission to
reuse an older row. An unrelated date query does not erase it.

1. Before a currently planned C is reached, a new supported start may move C only
   if it is observed before both the old and new C. Require stable game identity,
   pregame status, and unambiguous official evidence; record superseded bindings.
2. A new earlier C already in the past is `schedule_revision_missed_cutoff`. Do not
   generate an on-time forecast for it. A first-discovered game whose C already
   passed is likewise `first_seen_after_cutoff`.
3. The **first reached C is sticky**, whether that invocation succeeded, abstained
   or was missed. A later start change does not create a second forecast opportunity
   for that game/horizon/protocol. Historical simulation follows this same state
   machine; it must not choose the final schedule and work backwards.
4. A binding not renewed with fresh schedule/status evidence by C abstains. Latest
   known pre-cutoff postponed, cancelled, suspended, TBD, Live or Final status
   invalidates pregame eligibility. Never repair this with a later favorable status.
5. v1 conservatively abstains on explicit rescheduling/resumption flags, even where
   settlement mapping can retain game identity. A simple time revision without those
   flags can update a prepared binding under rule 1, but a book collected under a
   different start version cannot be reused. Current collector restrictions may mean
   there is no qualifying book; report that gap rather than broadening collection.
6. If a forecast already exists when a game is delayed/postponed/rescheduled, retain
   its original T and C. Report schedule drift separately. If identity is still the
   same and that exact contract finalizes binary, keep it in the original scoring
   cohort; do not remove a losing game because it was postponed. A cancellation or
   postponement alone never creates a Kalshi outcome. Replaced/ambiguous identity
   prevents scoring, with an explicit outcome-join exclusion.

### Recording and modes

`forward_shadow`: live command with system timestamps, pre-C witnessed bindings and
inputs, and durable forecast publication witnessed in `[C, C+150 seconds]` and
strictly before T. Record both insertion time and the post-commit publication receipt;
report `record_delay_seconds`. This is a T−60 **information** forecast recorded with
bounded delay, not a claim of execution exactly at T−60. Earliest invocation before C
only prepares: it does not substitute an early forecast for the specified horizon.

At C+150 precisely, timely publication is allowed; after it, record
`late_recording` abstention. The check is repeated after commit via the publication
receipt. If computation/commit crosses the boundary, retain the attempted probability
but exclude it as `late_publication`. A crash after decision commit but before receipt
leaves `publication_unconfirmed`; a later receipt cannot backdate it. Such attempts
are not successful forward-shadow forecasts. Latest evidence already available at
record time that the game started/cancelled/changed identity is an operational veto
(`record_time_veto`) with separate post-C references; it never improves the probability.
No proof of actual first pitch is claimed from a Preview status alone.

`historical_reconstruction`: explicit archive-only command, actual creation time now,
specified archived source ceiling and configuration. Replays the schedule/selection
policy from pre-cutoff information. Post-cutoff mapping cannot repair earlier forecast
eligibility. Strict and availability-unproven diagnostic reconstructions are separated.
Never call either forward-shadow, including reconstructions recorded before the event
but beyond the recording grace. Do not silently change an existing forward attempt's mode.

`synthetic`: any forecast with fixture/imported synthetic evidence, or injected clocks.
Fixture lineage is transitive through bindings and receipts, checked using source run
kind as well as declared mode. Mixed live/fixture evidence stays synthetic. Modes and
protocol hashes are never pooled in default evaluations.

Clock check: within a process compare UTC elapsed with monotonic elapsed; >2 seconds
disagreement, negative input ages or receipt-time regression => `clock_untrusted`.
Across restarts detect regressions against prior receipts; do not claim this detects
all clock skew. Trust in the Mac's synchronized clock is an explicit residual risk.

## 3. Candidate universe and deterministic contract identity

The ledger is built from **all archived official scheduled MLB games in the requested
original-date range** plus all discovered allowed-family contract/event candidates,
including unmapped ones. Freeze candidate references and discovery completion evidence
in the run's immutable provenance manifest; do not start from games with successful
books or finalized outcomes. Surface unmatched official games as `no_contract_known`,
and unmatched event candidates separately when no reliable game count exists.

For each game at its C, use the most recent complete, pre-C witnessed discovery/event
evidence covering it (status/rules age <=300 seconds). If discovery is partial, capped,
missing or too old, label `universe_incomplete` and abstain for affected games.
Still list every observed candidate. No archive proves the count of contracts/games
it never saw: report `unseen_candidate_count=unknown`, date coverage and collection gaps,
not 100% market coverage. Complete discovery also needs a pre-C receipt of the
completed run summary and its exact eligibility/page boundaries. Snapshot that
summary in the receipt because resumed runs can later mutate `runs.summary_json`;
a later successful completion cannot prove an earlier complete universe. A later
audit may append newly noticed missed candidates,
but cannot modify the original denominator or supply a forecast.

The scoring unit is `(MLB gamePk, horizon=3600, protocol/config cohort, run mode,
dataset namespace)`. Doubleheaders have distinct gamePk values and require a
consistent official game number/original start. The protocol fixes contract selection
before reading book prices, sizes, forecast success or outcomes:

1. Reuse reviewed aliases and full-game identity parsing. Require a pre-C prepared,
   witnessed identity binding: gamePk, original rule date/start, home/away IDs,
   official game number, YES team, reviewed terms hash and evidence tuple.
2. If multiple event listings map to the game, select the bytewise lexicographically
   smallest `event_ticker` among supported, identity-proven full-game listings.
3. Within that event, select the YES contract for the **lower numerical MLB team ID**.
   Require exactly the two distinct teams and unambiguous rule/label agreement; if
   duplicate tickers for the selected team, choose bytewise smallest ticker and log
   alternatives. If the lower-ID team's contract is missing, abstain; do not fall back
   to the other team's available book.
4. Validate open status, notional, rules and book only after selection. A closed or
   unusable selected contract causes abstention, not a switch to a favorable listing.
   Other contracts have disposition `opposing_contract` or `alternate_listing`.

The selected probability means **that contract's YES**. Do not average opposing
contracts, complement another contract's probability, select the tighter spread,
or treat opposing contracts as separate games. Repeated snapshots are inputs, never
additional evaluation samples. A database uniqueness constraint on the scoring unit
prevents duplicates, independent of the schedule-version ID or ticker.

Current gaps: collector eligibility lacks event-fetch/terms/version availability as
a complete durable binding; settlement mapping is generally established after play,
may use `prior`, and its first-fetch anchor can precede full evidence assembly. Add
F3 bindings without overwriting either journal. Persist all dependencies recursively,
including prior mapping references when used, and compute availability from the whole
tuple. Do not pass `journal.latest` as a pre-C mapping selector. A mapping established
after C may confirm the outcome join to the already frozen game/contract, but cannot
turn an identity abstention into a forecast or choose a different selected contract.

## 4. Probability and abstention policy

Apply common identity, schedule, universe, mode and timeliness checks to both
baselines. Constant emits exact `0.5` when those checks pass, regardless of book
availability; midpoint additionally requires the following book checks.

Select the **latest attempted completed book retrieval** for the chosen ticker
received and witnessed by C, ordered by retrieval time then `fetches.seq`.
Inspect parse-error/truncated/error attempts too. If the latest completed attempt
is unusable, abstain; do not search backward for an older attractive valid book.
A pending attempt is reported but is not a completed snapshot. No query may use
absolute nearest-time distance or a post-C response. A stale latest snapshot is
not replaced with a different contract's fresh one.

Requirements: successful untruncated response with verified blob hash; supported
parser; matching ticker/game/eligibility evidence; pregame at C; no schedule-start
revision between the book's eligibility proof and C; age in `[0,300]`; both YES and
NO bid sides present with at least one positive finite quantity. Validate every
level using current exact decimal rules (price 0..1, <=4 fractional digits;
quantity >=0, <=2 digits; no float, NaN, exponent or duplicate numerical price).
Zero quantity contributes no best level. In v1 any normalized flag, including
unsorted levels, fails closed as `book_flagged`; preserve individual flags.

For the verified $1 contract, let `bY=max(positive-quantity YES bids)` and
`bN=max(positive-quantity NO bids)` **from the same fetch**:

`askY = 1 − bN`; require `0 <= bY <= askY <= 1`.

`p_midpoint = (bY + askY) / 2` using Decimal with fixed precision 40 and
ROUND_HALF_EVEN. Division by two is exact for supported source precision (up to
five probability decimals). A locked book (`bY=askY`) is valid. A crossed book is
not clamped. Do not use last trade, market endpoint quotes, volume-weighted values,
bid-only estimates or sizes as probabilities. No extra spread/liquidity threshold
is introduced; such thresholds would alter coverage and require another protocol.

Persist all applicable reasons, with the first applicable stage as the primary
reason: provenance/clock → universe → identity → schedule/pregame → recording →
book. Within a stage use a versioned fixed reason order, not dictionary iteration.
Book reasons include `book_missing`, `book_fetch_failed`, `book_truncated`,
`book_parse_error`, `book_stale`, `book_side_missing`, `book_side_null`,
`book_side_empty`, `book_no_positive_quantity`, `book_invalid_price_or_quantity`,
`book_crossed`, `book_flagged`, `book_schedule_mismatch`, `archive_corrupt`.
Preserve input IDs even for rejected inputs. No probability is represented by zero
or 0.5 merely because the midpoint baseline abstained.

## 5. Outcomes, score definitions and reports

Evaluation takes an explicit `outcomes_as_of` UTC time plus a frozen source sequence
ceiling/manifest. This is an **archived retrieval-time boundary**, not a claim that
an evaluation was published at that historical time: keep actual creation time.
Require outcome `retrieved_at <= outcomes_as_of` and source membership in the
evaluation snapshot; retain visibility receipts when present. Do not apply this
weaker outcome-report convention to pre-C forecast input availability. For each
selected ticker use the latest completed settlement-market attempt by ingestion
sequence within that ceiling and time boundary, not the latest *finalized* result.
A later disputed/pending/malformed response invalidates current binary eligibility;
do not resurrect an older finalized label. Link the exact settlement observation,
version ID, revision, raw fetch, provider timestamps and retrieval time. New pending
attempts and refresh failures are reported; no promise of up-to-the-minute official
truth is made when refresh has not run. Repeated identical observations are new
retrieval evidence but not new samples.

For binary scoring require the selected contract's Kalshi interpretation to have
`status='finalized'`, `payout_kind='binary'`, no flags, `binary_outcome` yes or no,
valid nonmissing `settlement_ts`, notional exactly 1, and consistent exact payout
(YES→1, NO→0). Validate the outcome mapping against the frozen game and contract
identity. Do not infer y from MLB winner, an opposing contract, a provisional market
price, `determined`, `amended`, or an expiry time. The current verified parser does
not use `is_provisional` as a dispute flag; preserve that behavior unless reviewed
provider evidence establishes a different meaning.

`y=1` for YES, `y=0` for NO. Scalar payouts, **including scalar 0 or 1**, fractional
payouts and result/payout conflicts never become a binary label. Record exact payout
strings in excluded outcomes. MLB results remain an independent annotation; an MLB
disagreement is reported, not silently substituted or used to select the sample.

Outcome dispositions: `binary_scorable`, `pending`, `disputed`, `exceptional_payout`,
`invalid_or_missing_outcome`, `identity_unresolved`, `identity_changed`,
`outcome_fetch_failed`, `outcome_not_observed`. Cancellations with no finalized
contract result are pending/exceptional as the provider evidence dictates; never
assign 0, 0.5 or refund assumptions. Report provider status and detailed flags alongside
one primary disposition so counts add up, plus nonexclusive diagnostic counts.

For p in [0,1]:

- Binary Brier: `(p − y)^2` (single Bernoulli convention, range 0..1, no factor 2).
- Natural-log loss: `−[y*ln(q) + (1−y)*ln(1−q)]`,
  `q=min(1−epsilon,max(epsilon,p))`, epsilon=0.000001.
- Preserve p unchanged. Clip only for log loss and report number of clipped values.
  Use Decimal `ln`, precision 40, ROUND_HALF_EVEN; save per-event values as decimal
  strings. Means divide by the actual count; console display rounds to six decimal
  places only. N=0 gives null scores, not zero or NaN. Save Python/decimal runtime
  and evaluator version; reproducibility tolerance for recomputed log scores is
  1e−30 under the recorded arithmetic contract, not a claim of portable binary floats.

Compute primary metrics on the **same paired sample**: games where both baselines
have valid forecasts under the same protocol/mode and a scorable binary outcome.
Report mean per-game `midpoint_score − constant_score` for both metrics (negative
favors midpoint), N and per-game differences. Each game has weight 1. Report broader
constant-only results separately, with their different N and exclusions; never compare
that mean directly to the smaller midpoint mean.

Every report must include immutable cohort/forecast IDs and outcome-manifest digest;
mode, protocol/config/evaluator/code versions; original-date range; observed official
games; identity-resolved games; unresolved event groups (not guessed games); all
candidate contracts with dispositions; discovery gaps and completeness; common
eligible games; emitted/abstained counts per baseline and reason; timely/late/unconfirmed
publication counts; paired forecast count; paired binary-scored N; pending and excluded
outcomes; scalar/fractional counts; later mapping/schedule changes; clipping count;
age and record-delay distributions; runtime and cost.

Ratios: forecast coverage = valid forecasts / observed game opportunities; paired
coverage = both valid / observed game opportunities; outcome coverage = paired binary
N / both valid. Display numerators/denominators; zero denominator => null. Also show
conditional coverage among common-eligible games, clearly named. Unresolved events
and unknown unseen counts sit alongside those ratios rather than disappearing.
Finalized-game survival is not the candidate denominator. A report with no paired
binary outcomes is `inconclusive`, even if scores exist for synthetic fixtures.

Later settlement corrections append new evaluation runs with `supersedes_id`, the
same immutable forecast cohort, new outcome IDs and an explicit reason. Preserve
original forecasts and earlier reports. A→B→A retains three evaluation versions even
when A's payout reappears. Ordinary repeat evaluation with identical manifests,
as-of boundary, versions and config returns the existing evaluation. An observation
with different retrieval identity changes the evidence manifest, even if the payout
does not change. Select latest outcome evidence, never the best-performing revision.

Tiny samples support descriptive scores only: no claims of significance, calibration
quality, predictive edge or promotion. No automatic model selection. Runtime is
monotonic measured elapsed time, cost is external-call cost `0` for these local
baselines, and local compute cost is `unmeasured`, not falsely zero dollars.

## 6. Minimal additive persistence and invariants

Recommend explicit migration **003_forecast_evaluation.sql**, reserving schema 3
subject to integration review. Preserve all schema-1/2 tables and bytes. Five new
tables suffice; reuse `runs` for bounded execution/provenance. Proposed names below
are interfaces to implement, not existing SQL. IDs use INTEGER primary keys except
existing run/fetch UUID references. All JSON is canonical (`json_text`), versioned,
bounded and included in a content digest. Foreign keys use restrictive deletion.

| Table | Required record fields and constraints |
|---|---|
| `forecast_receipts` | id; subject kind (`fetch`, `binding`, `decision`, `discovery_run`); exactly one typed subject FK to `fetches.id`, `forecast_bindings.id`, `forecast_decisions.id`, or `runs.id`; subject digest; observed UTC; session ID; monotonic offset; observer code version; source mode; clock status; observing run FK. Snapshot payload for mutable discovery-run summaries. Unique subject + digest + observer namespace; first receipt for that content immutable. CHECK exactly one correctly typed subject FK; never accept arbitrary polymorphic strings without FK validation. |
| `forecast_bindings` | id, candidate key, revision, previous binding FK, created_at, run FK, policy/code/config hash, payload digest; nullable gamePk, event/selected ticker, team IDs, original date/start/game number, scheduled T and proposed C, status/reasons; evidence manifest JSON with typed source IDs, terms digest, eligibility/settlement mapping IDs if used and all prior dependencies. UNIQUE(candidate_key, policy/config, source-mode namespace, revision); unchanged semantic/evidence tuple returns existing binding. |
| `forecast_decisions` | id, scoring-unit idempotency key UNIQUE; run FK; mode/dataset/protocol; gamePk nullable for unresolved event groups; selected ticker/YES team nullable; horizon, T, C; created_at; binding FK; selection/code/config/baseline versions; frozen candidate/input manifest and hash; two baseline results (name, probability string or null, ordered abstention reasons, exact arithmetic inputs); supersedes FK for diagnostic corrections only. |
| `evaluation_runs` | id, idempotency digest UNIQUE, run FK, supersedes FK, created_at, outcomes_as_of, evaluator/runtime/config/code versions, immutable ordered forecast/cohort manifest, source ceiling, outcome manifest/digest, status and complete report JSON. No completed-report UPDATE. |
| `evaluation_items` | evaluation FK + decision FK composite PK; gamePk; outcome disposition/reasons; mapping/MLB/Kalshi version and observation references; exact payout/y nullable; two unrounded score strings or null, clipping indicators, paired deltas. UNIQUE(evaluation_id, gamePk, horizon) for resolved scoring units; no duplicate game via multiple candidate keys. |

Each JSON evidence reference also carries table/kind/parser version, fetch UUID/seq,
raw SHA-256, retrieved_at, receipt ID/availability bound and relevant entry digest.
Validate references and their semantic kind inside the insertion transaction; immutable
source tables make this safe. Prominent scalar FKs cover selected binding and source
receipts; nested manifest references require an integrity checker (SQLite does not
enforce JSON FKs). Store `schedule_version` as schedule fetch UUID + game-entry digest;
store forecast mapping version as binding ID + policy version, separately from any
settlement mapping revision. Avoid interpreting `Eligibility.mapping_version` as an ID.

The decision result JSON is a fixed pair, not an extensible agent framework; validate
exactly `constant-v1` and `midpoint-v1`, no duplicate names, decimal/null probabilities
and reason/probability consistency. This avoids redundant game identity rows while
retaining every requested forecast field. Store the run provenance's prompt/provider/
model/settings as null, not fabricated model calls. Source hash and installed package
version are authoritative code identities; optional Git SHA is supplementary.

Idempotency key = SHA-256(canonical tuple of dataset namespace, mode, protocol/config
hash, gamePk or unresolved event key, horizon). **Do not include invocation time,
snapshot ID, schedule revision or selected ticker**, which would permit retries to
create another forecast. A historical reconstruction campaign has an explicit fixed
dataset namespace and source ceiling, set before selection. Config changes require
a new protocol cohort, visibly separate; default reports never combine them.

Completed decisions, receipts, bindings and evaluations are append-only (reject
UPDATE/DELETE with SQL triggers). Retry conflict returns existing immutable result;
same key with differing payload is an idempotency conflict, not replacement.
Diagnostic forecast correction appends under a distinct correction namespace with
`supersedes_id`; default reports continue using the original published decisions.
A correction cannot inherit the earlier publication time or become its replacement
forward forecast. Outcome corrections use evaluation revisions, not forecast edits.

Indexes: `(subject FK, observed_at)` receipts; `(candidate_key, revision)` bindings;
unique scoring-unit key and `(mode, protocol, C, id)` decisions; `(created_at,id)`
evaluations. Add bounded lookup indexes on source request stage/key and retrieval
time if EXPLAIN on representative archives shows full-history scans; schedule blobs
may contain many games, so cache parsed objects only within a bounded invocation.

Transactions: take existing nonblocking canonical `writer_lock`; no new distributed
lock. Capture a consistent read snapshot and source ceilings (not just MAX seq of
requests still pending). Compute bounded pure projections; `BEGIN IMMEDIATE` inserts
each complete decision plus run checkpoint in one transaction. Receipt witnesses
require a subsequent read of committed rows. Evaluation computes against a frozen
manifest in bounded pages and writes one complete report/items transaction (max 1000
games); no externally visible half-report. On budget exhaustion roll back that report,
finish the ordinary run `partial`, and retain its manifest for deterministic retry.

No automatic retry on lock contention (CLI exit 1 with `writer_busy`); an operator
can retry the same command/key. SQLite wait remains capped at 5 seconds within the
command budget. Use a SQLite progress handler for long queries and check monotonic
deadline between items. No live API calls or file exports in write transactions.
Exports are generated from committed evaluation IDs after commit, and are not a
second source of truth. Run summaries may progress but cannot rewrite published facts.

| Invariant | Mechanism |
|---|---|
| No post-C probability input | Selection predicate on retrieved/observed times; transitive manifest validation; adversarial boundary tests |
| One game/horizon sample | Database scoring-unit and evaluation-item unique constraints, deterministic candidate selection |
| Earlier forecast immutable | Append-only triggers, conflict detection, separate correction namespace |
| Raw repeated retrieval identity retained | Fetch UUIDs in manifests, no body-hash observation deduplication |
| No partial published output | One transaction per complete decision/report; post-commit receipt distinguishes publication certainty |
| No synthetic/live mixing | Transitive source-mode validation and separate cohorts |
| Collector health independent | Explicit collection-kind filtering; new run kinds never satisfy collection health |
| Outcome corrections auditable | Exact version/observation IDs and superseding immutable evaluations |

## 7. Interfaces and scheduling boundaries

Proposed pure interfaces in new `src/vader_intelligence/forecast/` and
`src/vader_intelligence/evaluation/` packages:

- `forecast.policy.decide_schedule(history, protocol) -> ScheduleDecision`: no DB,
  no wall-clock call; explicit immutable observations with availability bounds.
- `forecast.selection.select_contract(candidates, binding) -> Selection` and
  `select_inputs(archive_view, cutoff, protocol) -> InputsOrAbstention`: deterministic
  order, complete rejection evidence, never calls a provider.
- `forecast.baselines.constant() -> Decimal`;
  `midpoint(book, notional) -> Decimal | InvalidBook`: pure arithmetic.
- `forecast.journal.prepare/record/witness`: only persistence layer; transactions,
  append-only keys, integrity checks and receipt/publication handling.
- `evaluation.outcomes.join(decisions, frozen_outcome_view) -> OutcomeItems`:
  provider identity/status policy isolated from scoring.
- `evaluation.scoring.brier(p,y)`, `log_loss(p,y,epsilon)`,
  `paired_summary(items)`: no SQL, clocks, configuration globals or HTTP.
- `evaluation.service.evaluate(manifest, as_of, budget) -> EvaluationResult`:
  freezes evidence then composes pure join/scoring and writes immutable results.

Proposed CLI, **not yet executable**; global `--db`/`--config` precede the subcommand:

```sh
vader --db data/research.sqlite3 forecast migrate
vader --db data/research.sqlite3 forecast tick --research-config research.toml --budget 15
vader --db data/research.sqlite3 forecast inspect --limit 20
vader --db data/research.sqlite3 forecast reconstruct --research-config research.toml \
  --from 2026-10-01 --through 2026-10-07 --source-ceiling 12345 --dataset archive-study-001
vader --db data/research.sqlite3 evaluate --forecast-manifest MANIFEST_RUN_ID \
  --outcomes-as-of 2026-10-09T00:00:00Z --budget 30
vader --db data/research.sqlite3 evaluation inspect --id EVALUATION_ID
```

`tick` is finite: witness preexisting completed evidence; prepare/revise future
bindings; freeze due decisions; record missed opportunities; exit. It must process
receipts before choosing sources and never let a receipt made now authorize an input
for an earlier C. Prior runs' incomplete pending fetches cannot be skipped forever
by a MAX-seq cursor: rescan pending IDs within a bounded tracked set. Exceeding the
bound is incomplete, not a hidden loss. Earliest runs need warm-up before any C.
Inspection uses read-only connections, keyset pagination and maximum limit 100.
Reconstruction is bounded to 31 days/1000 games per command and does not hit HTTP.
Exit 0 complete, 3 inconclusive (no paired binary outcomes/no due decisions),
1 failed/partial, 130 interrupted; reason-coded forecast abstentions can be a
successfully recorded complete batch and must not themselves fabricate an error.

Default F3 operational delivery is the finite commands and an isolated forward
demonstration, **no automatic launchd installation or change**. Current launchd
collects only; a 120-second collection cadence does not invoke forecasting. The
150-second record window tolerates one nominal 120-second research invocation cycle
plus 30 seconds, but no deployment SLA follows from it. Manual invocations can miss
it; that produces a recorded missed opportunity, not retrospective forward success.

Later scheduling, if separately authorized, should use one existing serialized
operations cycle with an archive-only forecast tick **before** collection, one at a
time, and no added provider requests. The tick examines books from prior passes.
Its budget must come from a remeasured combined wrapper budget; do not add 15 seconds
to an unchanged 90-second deadline and claim the same headroom. Keep collection and
forecast status separate. No second overlapping writer job or unbounded retry loop.
For manual settlement/evaluation batches, use the documented maintenance coordination
in `docs/integration.md`; forecast gaps during stopped collection remain visible.

### Compatibility and rollback (provisional shared interfaces)

| Reader/writer | Schema 1 | Schema 2 | Proposed schema 3 | Unknown future |
|---|---|---|---|---|
| Reference integration code | Supported collection | Supported collection/settlement | Reject | Reject |
| New F3 collector/ops | Preserve behavior | Preserve behavior | Explicitly supported after tests | Reject |
| New forecast/evaluation | Explicit migration required | Explicit migration required | Supported | Reject |

`forecast migrate` requires schema 2; on schema 1 instruct the operator to perform
the existing explicit settlement migration first. No migration on collection, tick,
health, inspection or replay. `db-init` still creates 1. Update `Store` guards,
settlement `require_schema/migrate` (2 and 3 supported, no downgrading), replay,
operations schema/column/count checks and installed-wheel tests together. Preserve
unknown-schema rejection. This is an additive expansion with no contraction or
automatic historical data backfill. Receipt backfills cannot create historical
availability. Verify populated 1→2→3 and 2→3 on copies.

Update core health to a positive allowlist of genuine collection/discovery/live-check
kinds; forecast/evaluation receipt runs must not mask collector failure. Preserve
the stricter operations `kind='collect'` classification. `forecast` runs must not
claim `books_this_pass` or reuse collection eligibility bounds.

Rollback: disable future research invocation, preserve all evidence, use compatible
schema-3 code or fix forward. The reference schema-2 binary is not a code-only rollback
for schema 3. Before migration, take an online backup and restore into a new path;
verify every table, raw hash, receipt/JSON reference and replay equality. Older backups
omit later evidence; never overwrite the current archive or drop tables/change
`user_version` to make old code run. Production migration/deployment is outside this
documentation task and requires the existing coordinated review/deployment process.

## 8. Small implementation steps and acceptance tests

Each step is one reviewable increment and leaves existing behavior intact. Tests
below are **planned**, not claims of execution in this documentation task.

| Step | Files and change | Independently verifiable completion |
|---|---|---|
| F3.1 Pure policies | New `forecast/policy.py`, `baselines.py`, `evaluation/scoring.py`; `tests/forecast/test_policy.py`, `test_baselines.py`, `tests/evaluation/test_scoring.py` | Injected-clock schedule state machine, exact midpoint and score golden cases; no IO/import side effects |
| F3.2 Expansion | `migrations/003_forecast_evaluation.sql`, new `forecast/schema.py`, `forecast/journal.py`; minimal `storage.py`, `settlement/schema.py`, `ops/archive.py` compatibility | Explicit idempotent migration and transactional rollback on injected DDL failure; old table fingerprints unchanged; 1/2/3 backups and future guards; append-only/uniqueness/JSON reference checks |
| F3.3 Evidence/selection | New `forecast/selection.py`, `forecast/service.py`; `tests/forecast/test_selection.py`, `test_journal.py` | Complete candidates, witnessed pre-C evidence, no future mapping, repeat/crash behavior, exact receipt bounds |
| F3.4 Forecast CLI | New `forecast/cli.py`, dedicated research TOML parser; minimal `cli.py` registration and health filtering; `tests/forecast/test_cli.py` | Bounded tick/reconstruct/inspect, clock/source-mode gates, package entry points, no implicit migration or HTTP |
| F3.5 Outcome/evaluation | New `evaluation/outcomes.py`, `service.py`, `journal.py`, `cli.py`; `tests/evaluation/test_outcomes.py`, `test_service.py` | Correct latest evidence, paired denominators, immutable corrections, finite arithmetic, manifest replay |
| F3.6 Integrated proof | Extend `tests/test_integration.py`, `tests/ops/test_operations.py`, `tests/integration/wheel_smoke.py`; later user/operator docs | Populated migration and full backup/restore/forecast+evaluation replay; unchanged collector behavior; fresh installed-wheel smoke; separate real forward evidence or explicit inconclusive outcome |

No edits to current collector eligibility semantics are required to invent coverage.
Shared integration-review touchpoints are schema validation, core health run kinds,
CLI registration/config loading, mapping evidence, replay order and operations backup.
After PR #4 review, compare those implementations with this pin, amend the spec if
necessary, and implement on a new code branch. Do not rebase or edit the integration
branch on behalf of this spec.

Focused test matrix (use isolated databases and explicitly synthetic evidence):

| Case | Required result |
|---|---|
| Response starts C−1s, arrives C+1ms; source timestamp older | Never selected; nearest post-C snapshot forbidden |
| Raw fetched C−10s but first visible receipt C+1ms | `availability_unproven`/no qualifying pre-C input, not rescued by sequence |
| Mapping prepared after C using old raw data | No forward forecast; only diagnostic reconstruction or outcome join |
| Schedule T changes before old/new C | New cutoff adopted, old plan retained; latest fresh supporting schedule required |
| Revision moves C into past; game first seen after C | Explicit missed-cutoff/first-seen abstention |
| Schedule change/postponement after frozen C | No refreeze/new game sample; retain original decision, annotate drift |
| Latest pre-C status Live/Postponed/TBD; missing game in covered schedule | Abstain despite older Preview record |
| Book age 300s vs 300s+1µs | First accepted, second stale; zero/future ages tested |
| Receipt/publication at C+150s vs +1µs | Timely vs late; before-C preparation alone never counts as publication |
| Crash before commit / after commit before receipt / after receipt | No row / unconfirmed publication / original successful result; no backdated receipt |
| Duplicate tick, competing processes, changed payload same key | One original decision; other writer rejected or returns same result; differing payload fails |
| Opposing books; duplicate listings; selected book missing | Lower-team-ID/lexical choice unchanged, one game, no fallback |
| Two doubleheader gamePk values, ambiguous identical starts | Separate proven games or quarantine; never date/team-only deduplication |
| Identical response body in two actual retrievals | Distinct input identities; same chosen observation on repeat deterministic replay |
| Missing/null/empty/zero-size side; crossed/unsorted/duplicate levels | Exact reason retained; midpoint abstains; broader constant coverage counted separately |
| Sub-cent values; p=0/1; locked book | Decimal exactness, bounded log clipping only, no Brier clipping |
| Later finalized→disputed, missing payout/time, scalar 0/1/0.53 | No fallback binary score; each exclusion visible |
| Settlement YES→NO→YES; MLB winner disagrees | Three immutable evaluation evidence versions; MLB never replaces Kalshi |
| Mapping later links to different game, or two decisions now join same game | Identity exclusion; unique game constraint prevents score duplication |
| Finalized record exists but latest attempt malformed | `outcome_fetch_failed`/invalid outcome; earlier good result not silently reused |
| Fixture with convincing old timestamps, or mixed lineage | Synthetic only; no forward-shadow counts |
| Corrupt blob or JSON reference, unsupported parser/schema | Fail closed with retained evidence, no partial publication |
| N=0, N=1, midpoint abstains but constant exists | Null paired scores at N=0; exact N, pending/exclusion/coverage counts; no significance claim |
| Sleep, wall regression, monotonic/UTC divergence, DB busy/full | Missed/clock-untrusted/failed explicit; no unlimited retries or fabricated forecasts |
| Schema-3 forecast success after failed collect | Collector remains unhealthy; operations still classifies only collection |

Future verification commands (after implementation, in its own checkout/environment):

```sh
.venv/bin/ruff check src tests ops
.venv/bin/ruff format --check src tests ops
.venv/bin/pytest -q tests/forecast tests/evaluation tests/test_integration.py
.venv/bin/pytest -q
.venv/bin/python -m unittest discover -s tests/ops -v
.venv/bin/uv build
# Install the built wheel with locked dependencies into a separate environment,
# then extend/run tests/integration/wheel_smoke.py outside the source checkout.
```

An eventual real forward demonstration needs receipts/bindings before C, a timely
published pair, and a later separate settlement refresh. If that opportunity does
not occur, report forward verification as **inconclusive**. Historical results and
fixtures cannot satisfy that criterion. Real calls, if later authorized, retain the
existing bounded reader and operational coordination; do not call live providers
from the pure forecast/evaluation tests.

## 9. Synthetic examples (not real observations)

All dates, game IDs, receipt times and outcomes in this section are **synthetic**.

**Normal:** fictional game 900001 starts 2026-10-01 20:00:00Z; C=19:00:00Z.
Binding is published/witnessed at 18:57:50Z. Schedule and book received at
18:58:00Z/18:58:20Z and witnessed at 18:58:30Z. At C the schedule is Preview,
identity/rules unchanged. Selected lower-ID team's best YES bid=0.4001 and NO
bid=0.5799, both with positive quantities. YES ask=0.4201; midpoint=0.4101.
Decision inserted at 19:00:12Z, publication witnessed at 19:00:13Z: record delay
13 seconds. With subsequent synthetic finalized YES payout 1.0000, Brier scores
are 0.25 and **0.34798201**, delta +0.09798201; constant log loss=ln(2), midpoint
log loss=−ln(0.4101). One paired game, not two opposing contracts.

**No future snapshot:** another book arrives 19:00:00.001Z at a much tighter spread.
It is forbidden. If the latest pre-C completed book has a missing NO side, midpoint
abstains rather than using the future book or an older valid one. Constant may remain
valid; paired N=0 gives null metrics and constant-only coverage is shown separately.

**Start revisions:** same fictional T=20:00. A supported update witnessed 18:30
moves T to 20:30 (new C=19:30): allowed before both cutoffs, provided identity and
new book/schedule evidence meet all rules. An update witnessed 19:05 that moves
T from 20:00 to 21:00 occurs after the first C: retain the 19:00 decision, even if
no scheduler invoked then. Do not invent a 20:00 replacement forecast. If instead
an 18:50 update moves T to 19:30, its C=18:30 is already past: missed-cutoff abstention.

**Late / historical:** first record made 19:03 for the original C=19:00 is beyond
150 seconds, although still before T. It is not a timely forward result. A separate
reconstruction created October 3 records that actual date and reconstruction mode;
pre-C raw timestamps alone cannot prove availability. A fixture using those same
timestamps remains synthetic.

**Corrections and exceptions:** evaluation E1 uses finalized YES revision 1 for
game 900001. A later finalized NO revision 2 yields E2 with `supersedes=E1`, the
same forecasts and y=0 (midpoint Brier=0.16818201). E1 is unchanged. A subsequent
scalar 0.5300 revision yields E3 with exceptional exclusion and no binary y; paired
scored N becomes zero with one exceptional outcome. An MLB winner does not fill it.

## 10. Design record, hazards and remaining decisions

Load assumption (estimate): 30 games/day × 2 baselines = 60 forecast outputs/day;
at roughly 8 KB per complete game decision+manifest, about 0.24 MB/day (~88 MB/year),
plus bindings, evaluation versions and receipts. Receipt volume follows ingestion,
not game count: at 30 books/120 seconds, 21,600 books/day × estimated 250 bytes/receipt
≈5.4 MB/day before indexes and other retrievals. Measure actual growth. Retain evidence
in v1; no automatic pruning. Main archive scans, not scoring arithmetic, constrain
10× growth. Index/cursor improvements and archived evaluation copies precede any
new server. One local SQLite database, one writer, zero extra network QPS suffice.

Local responsiveness target (not measured): a 100-game tick p95 <=5s and p99 <=10s
on this Mac after bounded indexed selection; hard budget 15s. Report measured
distribution later; do not average percentiles. Missed deadlines reduce coverage.
No availability SLA; integrity takes precedence over an on-time forecast. Committed
rows retain SQLite FULL durability assumptions; host/disk loss is outside local
redundancy. RPO/RTO depend on verified backups and are unmeasured.

| Choice | Rejected alternative and reason |
|---|---|
| Existing SQLite + immutable records | A server/queue adds operations at ~60 outputs/day without improving temporal truth |
| Conservative availability receipts | HTTP timestamps/sequence masquerading as commit or mapping availability permits leakage |
| One sticky game/horizon decision | Refreezing after postponements or failed forecasts selects favorable opportunities |
| Fixed contract selection before book tests | Best spread/liquidity/outcome selection changes the sample and can hide failures |
| Paired scoring with full exclusion ledger | Comparing differently covered samples confounds forecast accuracy and coverage |
| Append corrected evaluation | Updating scores in place erases what earlier reports actually asserted |
| Finite manual CLI first | A second permanent schedule introduces overlap and deployment scope before runtime is measured |

Hazards applicable to this design (Cool Coder data-systems catalog):

| Hazard | Enforcement / failure and recovery |
|---|---|
| H-01/02/03/04 lost update, skew, uniqueness, assumed isolation | Shared local writer lock + explicit BEGIN IMMEDIATE + database unique keys; competing-process tests; retry reads existing result |
| H-07/08/42 long reads, unbounded transaction/result | Bounded date/game limits, keyset pages, source manifests, progress-handler deadlines; partial run on cap, no truncated success |
| H-09/12 breaking schema/blocking DDL | Explicit additive migration in coordinated maintenance; copies and old-reader refusal verified; no implicit migration |
| H-14/18 retry and unknown completion | Deterministic key, atomic decision, separate publication witness; unknown stays unknown, never backdated |
| H-20/21/36 clocks and processing windows | UTC for cutoff, monotonic for elapsed; sequence for ingestion; receipt checks; explicit late policy |
| H-32 dual write | SQLite source of truth; exports derived only after commit |
| H-37/38 nondeterminism/destructive backfill | Frozen manifests, versioned pure functions, append-only triggers; no live lookup or in-place correction |
| H-39 overlapping jobs | Nonblocking same DB writer lock; finite commands; any future schedule serialized |
| H-46/47/48 rollback, integrity visibility, untested failures | Compatible-code rollback only; receipt/reference/denominator checks; crash/restore tests in F3.2/F3.6 |

No new authentication, secret, external endpoint or executable input boundary.
Archive data remains untrusted: reuse bounded identifiers/decimal parsing, verify
hashes, parameterize SQL and bound JSON/manifest sizes (10 MiB per manifest, reject
overflow). Local operator can edit the machine/database: this is reproducible
provenance, not a tamper-proof publication service. Do not claim cryptographic proof
of a forward forecast against a malicious operator or undetected clock skew.

Open integration decisions, with defaults:

1. **PR #4 review changes:** exact compatibility/health fixes are unknown until review
   completes. Default: reconcile its final reviewed commit before code work; do not
   silently implement against a moving branch or broaden schema guards.
2. **Migration number:** reserve 003/schema 3; if review consumes that number, renumber
   once and update the matrix/tests. Never reuse an existing migration number.
3. **Legacy availability:** insufficient existing durable-visibility proof may leave
   strict reconstruction coverage near zero. Default: keep diagnostic results separate
   and start prospective receipts; do not loosen temporal rules to obtain a score.
4. **Freshness/record delay:** recommended 300s/150s are explicit research defaults,
   not validated optimal parameters. Measure coverage/delay without tuning on outcomes;
   any change starts a separate protocol.
5. **Production scheduling:** not selected or authorized here. Default: finite commands
   and controlled forward demonstration first; later remeasure a single serialized
   schedule. Existing collector service status must be inspected anew at deployment.

Skill provenance: `planner`, `system-design`, `data-systems-design`, `detail-planning`,
Cool Coder engineering-skills **1.2.0**, pin
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. The 24 files in these installed folders
matched the pinned upstream archive on a read-only comparison. No installations
were changed. The user's requested document paths take precedence over the skills'
default plan/executor persistence paths. This specification stops at milestone 3.
