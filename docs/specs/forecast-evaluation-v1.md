# Milestone 3: baseline forecasts and evaluation v1

Status: implementation specification, **not implemented**. Written 2026-09-30
against integration commit `9d3d1ea2f167d5154dd6c67b3f47f2c7640fced7`.
PR #4 is independently under review. All proposed shared-interface changes below
are provisional until that review is reconciled. This document and the
[handoff](../handoffs/forecast-evaluation-spec.md) are the requested F3 planning
artifacts; historical `plan.md` and `executor.md` remain unchanged.
Revised from documentation commit `3c49998ac8356ec9c4d974292e2c4f8e03b92d82`
to resolve the three specification review findings. Integration head
`9a34420947f5722109186ff4897f0b1f6cb18f09` is under independent re-review;
this documentation revision neither changes nor claims to revalidate it.

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
| `collector.py:Collector.run`, `pass_summary` | Discovery/eligibility and book requests currently interleave; completion is summarized at invocation end. F3 requires an immutable discovery-phase summary committed before any book request, independent of book success (§3). This is a future interface change, not existing behavior or a PR #4 edit. |
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
| `record_grace_seconds` | 150 after cutoff for witnessed operational publication; record actual delay separately from cutoff eligibility |
| `log_epsilon` | Exact string `0.000001`; evaluation configuration, not a probability edit |
| `max_games` / `max_contracts` | 100 / 200 per invocation; exceed either => explicit incomplete manifest, never truncate silently |
| `budget_seconds` | 15 for one offline tick; 30 for evaluation, including database work |

Keep 300 seconds as the maximum research age, not a collection-service guarantee.
It does **not generally tolerate one missed cycle**: cutoff offset, collection
duration and the next tick's visibility receipt can leave the newest qualifying
book older than 300 seconds. The 120-second cadence alone cannot bound that age.
Retain tick-before-collection ordering and accept `book_stale` when this happens.
No extra witnessing phase or enlarged freshness window is part of v1.
The choice does not prove that a five-minute-old market is current. Persist retrieval
age and witnessing delay separately. Freshness remains a versioned configuration
value fixed at 300 seconds for this v1 cohort; do not change it to improve observed
coverage. A future protocol change needs demonstrated need and separate review,
not automatic tuning. Reject invalid/nonfinite settings and configuration drift
within the cohort. Publication grace remains 150 seconds, independent of input age.

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
has read a previously committed fetch/binding/publication record or immutable
discovery-phase summary, sample real UTC and monotonic
time and append a receipt with that observed time and subject digest. That time is
a conservative upper bound on availability, not an invented exact commit timestamp.
The read and observation precede receipt insertion. Never write a pre-commit timestamp
and call it committed. No user-supplied wall time is allowed in live commands.
Receipts use a local session ID and clock checks, and cannot be imported as live proof.

`forecast tick` witnesses completed retrievals and discovery-phase summaries from
prior collection passes, prepares bindings, and witnesses committed bindings within
that tick. It still runs **before** the next collection pass; do not add a post-book
or mid-collection witnessing phase. Future ticks compute cutoff decisions using only
receipts whose observation times are <= C. This is all local: no network call inside
any transaction. Persisting the discovery summary before books is not itself a receipt;
the existing next tick observes it, even if that pass's book stage failed or is incomplete.
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
   passed is likewise `first_seen_after_cutoff`. Here first discovery means actual
   evidence availability, not the later invocation that reads it: a late invocation
   with an already witnessed pre-C binding does not acquire this exclusion.
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
6. When a game is delayed/postponed/rescheduled after C, retain its original T, C and
   cutoff-defined research population even if publication never occurred. Report
   drift separately. If identity is still the same and that exact contract finalizes
   binary, it can be scored in the cutoff-reconstruction view; actual forward scoring
   additionally requires successful publication (§5). Do not remove a game from the
   research population because it was postponed. A cancellation or
   postponement alone never creates a Kalshi outcome. Replaced/ambiguous identity
   prevents scoring, with an explicit outcome-join exclusion.

### Three independent decisions: cutoff, publication, settlement

1. **Cutoff information:** freeze common eligibility, per-baseline abstention reasons
   and probabilities using only qualifying evidence available by C. This is a pure
   function of that evidence and the protocol, independent of when computation or
   publication is attempted. Recording a cutoff decision later does not backdate its
   creation time. No post-C status, warning, invocation delay or publication failure
   may alter these fields or remove a game from the cutoff-defined population.
2. **Operational publication:** record actual attempt time, durable publication
   receipt/time, delay, status, and any veto with its own evidence references. A
   post-C postponement, cancellation, start or identity warning may veto publication,
   but cannot change cutoff eligibility, abstention reasons or frozen probabilities.
3. **Settlement:** later outcome joins determine whether the selected contract has
   an eligible final binary label. Scoring exclusions belong here (§5), not in either
   earlier decision. An identity warning may later clear or remain an outcome-join
   exclusion; neither case edits the research population or its probabilities.

`forward_shadow` is an execution/cohort mode, **not a success flag**. Only a live
attempt with system timestamps and durable publication witnessed in `[C,C+150s]`,
strictly before the known T, and no operational veto counts as `published_timely`.
Store decision insertion time separately from publication time. Publication here means
explicitly releasing the immutable baseline result pair in the local research ledger;
merely computing/storing a cutoff decision is not publication. A pair may contain a
midpoint abstention; publication does not turn that abstention into a probability.
Before C a tick only prepares; it cannot publish an early substitute for T−60.

At C+150 exactly, timely publication is allowed. After that, publication is `missed`
(reason `late_invocation`), not an information abstention. If an allowed attempt's
commit/receipt crosses the boundary, it is `late_publication`; if no receipt survives,
`publication_unconfirmed`. Operational vetoes are `vetoed` with reason and post-C
evidence (including observation/availability time); write failures are `failed` where
failure evidence exists. No attempt is `not_attempted`, not implicitly successful.
These statuses leave cutoff probabilities intact. Latest already available operational
evidence is checked immediately before the allowed publication record is committed;
no additional HTTP request or witnessing phase is added. Unknown warnings not yet
observed cannot be acted on, and Preview alone is not proof of actual first pitch.
Successful publication is not retroactively revoked by a later warning; append the
warning/settlement evidence. A retry returns the original publication outcome and
cannot choose a more favorable time or publish a replacement after a veto (§6).

`historical_reconstruction`: explicit archive-only command with actual creation time,
archived source ceiling and configuration. Recompute the schedule/selection policy
from qualifying pre-C evidence, **ignoring actual invocation/publication timing and
post-C vetoes for information eligibility**. A genuinely missing pre-C binding or
availability proof remains missing; a later mapping cannot repair it. Publication status
is `not_applicable` for these research reconstructions. Separate strict qualifying
results from availability-unproven diagnostics. No reconstruction, unpublished decision,
late attempt or veto is relabeled as a successful forward forecast. A separate
reconstruction can reference a failed forward opportunity without rewriting its mode.

`synthetic`: any forecast with fixture/imported synthetic evidence, or injected clocks.
Fixture lineage is transitive through bindings and receipts, checked using source run
kind as well as declared mode. Mixed live/fixture evidence stays synthetic. Modes and
protocol hashes are never pooled in evaluations. Synthetic tests may simulate either
view but must retain synthetic provenance in both.

Clock check: within a process compare UTC elapsed with monotonic elapsed; >2 seconds
disagreement, negative input ages or receipt-time regression => `clock_untrusted`.
Across restarts detect regressions against prior receipts; do not claim this detects
all clock skew. Trust in the Mac's synchronized clock is an explicit residual risk.
Untrusted pre-C input timestamps affect cutoff-information eligibility; a clock failure
only during later publication affects publication status, not the frozen cutoff facts.

## 3. Candidate universe and deterministic contract identity

### Future collector phase boundary (required, not implemented)

Refactor a bounded collection pass into discovery/identity/eligibility followed by
books. This is necessary because the reference collector interleaves them, and a
book exception can prevent later candidates from being checked. Do not treat its
existing end-of-run `summary_json` as the proposed independent discovery fact.

1. Using the existing HTTP bounds and pass deadline, finish allowed-family pagination,
   series/terms, event membership and official schedule reads. Classify every discovered
   candidate for identity/scope/pregame eligibility, without requesting any book.
   Use existing configured page/market limits, further bounded by the research caps;
   do not raise limits as part of this separation. No network call is inside a write
   transaction. Phase failure or cap exhaustion is explicitly incomplete.
2. Before the first book request, atomically persist one immutable `discovery_passes`
   summary for this invocation/session. It contains:
   - `pass_id`, `run_id`, session identity, phase start/completion times, summary schema
     version, collector/parser/mapping policy versions, code/config hashes and digest;
   - exact series/document/event/schedule retrieval IDs and raw hashes, candidate
     market source IDs and ordered pagination attempts/checkpoints/cursor chain;
   - requested family/date window, page count, terminal-cursor evidence, configured
     caps, truncation/cursor-loop/failure reasons, and `pages_complete`;
   - candidate contract/event counts, deduplicated ticker list, observed official-game
     IDs/count, unmatched events/games, plus the complete candidate membership manifest;
   - `eligibility_after_id` exclusive and `eligibility_through_id` inclusive, filtered
     by run/pass, exact eligibility-row IDs and their source tuples, checked candidate
     count, eligible contract/game counts, all exclusions/ambiguities and reason counts;
   - `eligibility_complete` (every enumerated candidate has a decision) and `complete`
     (exhaustive unambiguous membership, all required sources and decisions present).
   Counts must reconcile with the manifest. Membership ambiguity/missing sources fail
   the affected discovery scope closed. A fully enumerated but identity-ambiguous
   candidate can have an explicit completed exclusion; it is never eligible merely
   because the phase finished. Empty complete discovery is distinct from unknown scope.
3. Only a complete committed phase enables its eligible candidates' book stage.
   Before each book attempt preserve the existing fresh-schedule/status and T−2
   guards; append any renewed book-specific eligibility proof separately. These
   checks can stop a book without changing the earlier phase summary or its row range.
   Book errors, deadline exhaustion and overall `runs.status='partial'` are independent
   book/pass outcomes, not updates to discovery completeness.
4. The existing **next pre-collection forecast tick** reads the committed summary and
   appends its availability receipt, linking `pass_id` and digest with actual observed
   UTC/session/clock evidence. This is not a new witnessing phase. Completion time
   alone is not availability proof. Require that receipt and supporting input receipts
   by C; if too late or stale, abstain. No requirement that the book stage or whole run
   has completed successfully. The latest applicable incomplete discovery summary
   fails closed; do not skip it in favor of an older complete one.

On interruption before a summary commits, no complete discovery fact exists; retain
raw/checkpoints and surface the missing phase. A discovery fetch witnessed before C
whose pass has no committed summary establishes incomplete discovery for its scope;
do not fall back to an older complete pass. A merely post-C attempt cannot invalidate
a pre-C phase. A resumed invocation gets a new pass ID
and fresh discovery, without rewriting a prior complete or incomplete summary.
Order summaries by their witnessed ingestion/phase sequence, not mutable run finish
time. Unique pass IDs and immutable digests make retries idempotent. Schema 1/2 paths
retain their existing behavior; this new interface is explicitly enabled on schema 3.

### Population and contract selection

The ledger is built from **all archived official scheduled MLB games in the requested
original-date range** plus all discovered allowed-family contract/event candidates,
including unmapped ones. Freeze candidate references and discovery completion evidence
in the run's immutable provenance manifest; do not start from games with successful
books or finalized outcomes. Surface unmatched official games as `no_contract_known`,
and unmatched event candidates separately when no reliable game count exists.

For each game at its C, use the most recent pre-C witnessed discovery-phase summary
covering it, requiring completeness and fresh event evidence (status/rules age <=300
seconds). If that discovery is partial, capped, ambiguous,
missing or too old, label `universe_incomplete` and abstain for affected games.
Still list every observed candidate. No archive proves the count of contracts/games
it never saw: report `unseen_candidate_count=unknown`, date coverage and collection gaps,
not 100% market coverage. Use the immutable phase summary and receipt above, not
mutable `runs.summary_json` or successful book completion. A later successful
completion cannot prove an earlier complete universe. A later audit may append
newly noticed missed candidates,
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
4. Validate open status, notional and rules only after selection; failures affect both
   baselines. Validate the book additionally for midpoint only. Book failure cannot
   invalidate constant eligibility or switch selection to a favorable listing.
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

Apply common **cutoff-information** identity, schedule, universe, source-mode and
availability checks to both baselines. Invocation time, publication status and later
vetoes are not common eligibility checks. Constant emits exact `0.5` when the common
checks pass, regardless of book success or whole-run status. Midpoint additionally
requires the following book inputs. Later publication/settlement fields never erase
either baseline's stored cutoff probability.

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
cutoff reason: pre-C provenance/clock → universe → identity → schedule/pregame →
book. Publication delays/vetoes and settlement exclusions have separate reason lists,
not entries in this order. Within a stage use a versioned fixed reason order, not dictionary iteration.
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

### Two evaluation views; never pool them

**`cutoff-reconstruction` — cutoff-based historical reconstruction.** Recompute from
qualifying pre-C evidence under a fixed protocol. Probability/abstention and inclusion
in the cutoff-defined population do not depend on actual invocation time, publication
success or post-C vetoes. Require eligible final settlement before scoring. Label every
score as reconstruction, even if a corresponding forward opportunity exists; neither
copying nor recomputing a cutoff result proves publication. Publication metadata may be
shown as an annotation, but is not a filter for this view.

**`forward-shadow` — actual forward performance.** Same cutoff-defined opportunity
population and information requirements, plus evidence that a probability was actually
published timely without a veto. Score only those published probabilities with eligible
final settlements. Include every missed, failed, vetoed, late and unconfirmed opportunity
in the population and coverage report. Do not impute scores to these failures or move
them to information abstentions. Successful publication with a midpoint abstention counts
as a published constant only, not a published midpoint. A later warning cannot revoke a
successful earlier publication to improve scores. Settlement exclusions still apply.

Each evaluation freezes `view`, compatible record mode, protocol, population and
outcome evidence. Reject mixed-mode/view manifests, rather than silently pooling or
dropping incompatible rows. A reconstruction for a failed forward opportunity is a
separate, labeled record/evaluation; the failed forward record remains failed. Synthetic
evaluations name the simulated view and always show `source_mode=synthetic`.

Within **each** view, paired metrics use the same games for constant and midpoint:
both meet that view's forecast requirements and have a scorable binary outcome.
Report mean per-game `midpoint_score − constant_score` (negative favors midpoint), N
and per-game differences, weight 1/game. Report broader constant-only results separately,
with their N/exclusions. Never compare that mean to a smaller midpoint sample as a paired
result, nor compare the views' potentially different samples as publication-independent
accuracy. No publication or settlement filter changes the original population manifest.

Coverage definitions (numerators/denominators mandatory, zero denominator => null):

| Quantity | Definition |
|---|---|
| `U` | Observed game opportunities in the pre-cutoff population, including information exclusions; no filtering by publication or settlement |
| `E` | Common cutoff-information eligible games (independent of books) |
| `K`, `M`, `P` | Cutoff-valid constant, midpoint, and paired counts respectively; K=E; P is their intersection |
| Cutoff coverage | K/U, M/U, P/U, plus clearly labeled conditional ratios K/E and M/E |
| `FK`, `FM`, `FP` | Actual timely published constant, midpoint and paired counts; forward view only, each a subset of the corresponding cutoff-valid count |
| Publication coverage | FK/K, FM/M, FP/P and unconditional FK/U, FM/U, FP/U; show veto/failed/missed/late/unconfirmed/not-attempted counts and all reasons separately |
| Binary outcome coverage | Reconstruction: paired scored N/P. Forward: paired scored N/FP. Also show settlement eligibility/pending/exclusions for the full cutoff-valid P so publication filters cannot hide them |

Persist one primary information reason, one primary publication status (plus all veto
flags), and one primary settlement disposition per opportunity. These are orthogonal
counts, not one combined exclusion funnel. Every report also includes immutable
population/decision IDs and outcome-manifest digest; view/mode, config/evaluator/code
versions; date range; unresolved event groups (not guessed games); all candidate
contracts/dispositions; immutable discovery-phase IDs, completeness, gaps and unknown
unseen counts; binary/exceptional/pending counts; later mapping/schedule changes;
clipping count; retrieval ages, witness delays, publication delays; runtime/cost.
An unattempted opportunity absent from the decision table is still counted from the
frozen population manifest, never silently dropped by an inner join.
For unattempted opportunities, derive E/K/M/P as explicitly labeled cutoff-policy
reconstruction annotations from that manifest. These annotations do not insert a
successful forward decision, supply a forward probability or enter forward scores;
FK/FM/FP still require actual publication evidence. Keep any scored reconstruction
in its own view and compatible record mode.

Finalized-game survival and publication success are not candidate denominators.
N=0 is `inconclusive` for that view's paired scores; it does not imply U=0 or erase
coverage failures. An unresolved final identity or exceptional payout may exclude
scoring in both views, but cannot remove that opportunity or rewrite its probability.

Later settlement corrections append new evaluation runs with `supersedes_id`, the
same immutable forecast cohort, new outcome IDs and an explicit reason. Preserve
original forecasts and earlier reports. A→B→A retains three evaluation versions even
when A's payout reappears. Ordinary repeat evaluation with identical manifests,
as-of boundary, view, versions and config returns the existing evaluation. An observation
with different retrieval identity changes the evidence manifest, even if the payout
does not change. Select latest outcome evidence, never the best-performing revision.

Tiny samples support descriptive scores only: no claims of significance, calibration
quality, predictive edge or promotion. No automatic model selection. Runtime is
monotonic measured elapsed time, cost is external-call cost `0` for these local
baselines, and local compute cost is `unmeasured`, not falsely zero dollars.

## 6. Minimal additive persistence and invariants

Recommend explicit migration **003_forecast_evaluation.sql**, reserving schema 3
subject to integration review. Preserve all schema-1/2 tables and bytes. Seven new
tables suffice; reuse `runs` for bounded execution/provenance. The two additions to
the initial spec are the independent discovery summary and publication outcome;
they separate facts with different completion times rather than adding a workflow
engine. Proposed names below
are interfaces to implement, not existing SQL. IDs use INTEGER primary keys except
existing run/fetch UUID references. All JSON is canonical (`json_text`), versioned,
bounded and included in a content digest. Foreign keys use restrictive deletion.

| Table | Required record fields and constraints |
|---|---|
| `discovery_passes` | id/phase sequence, unique `(run_id, session_id)`, run FK, created/completed times, summary schema/code/config versions, immutable §3 manifest/counts/source references/eligibility bounds/completion flags/reasons and digest. No book-success or whole-run-success prerequisite. No UPDATE on resume. |
| `forecast_receipts` | id; subject kind (`fetch`, `binding`, `publication`, `discovery_pass`); exactly one typed subject FK to `fetches.id`, `forecast_bindings.id`, `forecast_publications.id`, or `discovery_passes.id`; subject digest; observed UTC; session ID; monotonic offset; observer code version; source mode; clock status; observing run FK. Unique subject + digest + observer namespace; first receipt for that content immutable. CHECK exactly one correctly typed subject FK; never use mutable whole-run summaries as discovery proof. |
| `forecast_bindings` | id, candidate key, revision, previous binding FK, created_at, run FK, policy/code/config hash, payload digest; nullable gamePk, event/selected ticker, team IDs, original date/start/game number, scheduled T and proposed C, status/reasons; evidence manifest JSON with typed source IDs, terms digest, eligibility/settlement mapping IDs if used and all prior dependencies. UNIQUE(candidate_key, policy/config, source-mode namespace, revision); unchanged semantic/evidence tuple returns existing binding. |
| `forecast_decisions` | id, scoring-unit idempotency key UNIQUE; run FK; mode/dataset/protocol; gamePk nullable for unresolved event groups; selected ticker/YES team nullable; horizon, T, C; actual created_at; binding and discovery-pass FKs; selection/code/config/baseline versions; frozen candidate/input manifest and hash; common `cutoff_eligible`/reasons; two baseline results (name, cutoff probability string or null, cutoff abstention reasons, exact arithmetic inputs); supersedes FK for diagnostic corrections only. No publication/outcome facts in information eligibility. |
| `forecast_publications` | id, decision FK UNIQUE for the single logical primary publication, run FK, actual attempt/created times, decision digest, verdict (`allowed`, `vetoed`, `missed`, `failed`), publication-policy version, operational reasons and evidence references/observed times (post-C allowed). Append-only. Only `allowed` plus a timely post-commit receipt qualifies as `published_timely`; otherwise derive `late_publication` or `publication_unconfirmed`. No row means `not_attempted`; reconstruction is `not_applicable`. Probability values remain solely in the referenced immutable decision. |
| `evaluation_runs` | id, idempotency digest UNIQUE, run FK, supersedes FK, created_at, outcomes_as_of, **view** and compatible source mode, evaluator/runtime/config/code versions, immutable ordered population/decision manifest (including unattempted opportunities), source ceiling, separate publication and outcome manifests/digests, status and complete report JSON. No completed-report UPDATE. |
| `evaluation_items` | evaluation FK + opportunity key composite PK; nullable decision FK for unattempted opportunities, gamePk; separate cutoff status/reasons, publication status/reasons/record+receipt references, and outcome disposition/reasons; mapping/MLB/Kalshi references; exact payout/y nullable; view-appropriate score strings or null, clipping indicators, paired deltas. UNIQUE(evaluation_id, gamePk, horizon) for resolved scoring units; no duplicate game via multiple candidate keys. |

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
and cutoff-reason/probability consistency. Publication veto or late recording cannot
make a non-null cutoff probability invalid or null. This avoids redundant game identity rows while
retaining every requested forecast field. Store the run provenance's prompt/provider/
model/settings as null, not fabricated model calls. Source hash and installed package
version are authoritative code identities; optional Git SHA is supplementary.

Idempotency key = SHA-256(canonical tuple of dataset namespace, mode, protocol/config
hash, gamePk or unresolved event key, horizon). **Do not include invocation time,
snapshot ID, schedule revision or selected ticker**, which would permit retries to
create another forecast. A historical reconstruction campaign has an explicit fixed
dataset namespace and source ceiling, set before selection. Config changes require
a new protocol cohort, visibly separate; default reports never combine them.

Discovery summaries, decisions, publication records, receipts, bindings and evaluations
are append-only (reject
UPDATE/DELETE with SQL triggers). Retry conflict returns existing immutable result;
same key with differing payload is an idempotency conflict, not replacement.
One primary publication record per decision prevents retries from selecting a later,
more favorable operational state. An `allowed` record without a receipt can be witnessed
on retry at the **actual** later time; no backdating or replacement verdict. Additional
warning evidence remains in the raw archive and separate settlement/publication report
annotations, not an UPDATE or a second primary publication. A failure before the primary
record commits is `not_attempted`/unknown in the ledger unless run diagnostics prove
failure; diagnostics cannot prove publication. Reconstruction creates no publication
record. Evaluation idempotency includes view and all three evidence manifests.
Diagnostic forecast correction appends under a distinct correction namespace with
`supersedes_id`; default reports continue using the original cutoff decisions and
their actual publication dispositions.
A correction cannot inherit the earlier publication time or become its replacement
forward forecast. Outcome corrections use evaluation revisions, not forecast edits.

Indexes: `(subject FK, observed_at)` receipts; `(candidate_key, revision)` bindings;
unique scoring-unit key and `(mode, protocol, C, id)` decisions; unique publication
decision FK; `(run_id,session_id)` discovery summaries; `(created_at,id)`
evaluations. Add bounded lookup indexes on source request stage/key and retrieval
time if EXPLAIN on representative archives shows full-history scans; schedule blobs
may contain many games, so cache parsed objects only within a bounded invocation.

Transactions: take existing nonblocking canonical `writer_lock`; no new distributed
lock. Capture a consistent read snapshot and source ceilings (not just MAX seq of
requests still pending). Compute bounded pure projections; `BEGIN IMMEDIATE` inserts
each complete cutoff decision plus run checkpoint in one transaction. Discovery
summary commits separately before books. Publication verdict commits separately after
its operational guard, referencing the committed decision; a veto never rolls back
that decision. Receipt witnesses require a subsequent read of committed rows within
the existing tick. Evaluation computes against a frozen
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
| Publication cannot change cutoff facts | Separate immutable decision/publication rows and reason domains; view-specific evaluation filters |
| Book failure cannot change discovery | Pre-book immutable phase summary and own receipt; book/run status excluded from common eligibility |
| No partial published output | Complete decision precedes publication; post-commit publication receipt distinguishes certainty; atomic complete reports |
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
  append-only cutoff decisions/bindings and integrity checks.
- `forecast.publication.decide(decision, actual_now, operational_evidence) -> Verdict`:
  cannot edit or recompute the cutoff result; persist publication separately through
  the journal and witness its allowed record. No new provider requests.
- `collector.py` discovery-phase builder and storage insertion interface: immutable
  `DiscoveryPassSummary` as specified in §3, committed before book iteration; research
  selection reads that fact, not the final collection-run status.
- `evaluation.outcomes.join(decisions, frozen_outcome_view) -> OutcomeItems`:
  provider identity/status policy isolated from scoring.
- `evaluation.scoring.brier(p,y)`, `log_loss(p,y,epsilon)`,
  `paired_summary(items)`: no SQL, clocks, configuration globals or HTTP.
- `evaluation.service.evaluate(manifest, view, as_of, budget) -> EvaluationResult`:
  freezes evidence then composes pure join/scoring and writes immutable results.

Proposed CLI, **not yet executable**; global `--db`/`--config` precede the subcommand:

```sh
vader --db data/research.sqlite3 forecast migrate
vader --db data/research.sqlite3 forecast tick --research-config research.toml --budget 15
vader --db data/research.sqlite3 forecast inspect --limit 20
vader --db data/research.sqlite3 forecast reconstruct --research-config research.toml \
  --from 2026-10-01 --through 2026-10-07 --source-ceiling 12345 --dataset archive-study-001
vader --db data/research.sqlite3 evaluate --forecast-manifest MANIFEST_RUN_ID \
  --view cutoff-reconstruction --outcomes-as-of 2026-10-09T00:00:00Z --budget 30
# A separate evaluation of a forward-mode manifest uses --view forward-shadow.
vader --db data/research.sqlite3 evaluation inspect --id EVALUATION_ID
```

`tick` is finite: witness preexisting completed evidence and discovery summaries;
prepare/revise future bindings; compute due cutoff decisions; attempt operational
publication separately; record missed opportunities; exit. It must process
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
150-second publication window is a policy limit, not a promise that a 120-second
cycle will meet it. Invocation and commit latency can miss it. Such misses change
actual forward publication coverage, not cutoff information eligibility or qualifying
reconstruction probabilities. They never imply retrospective forward success.

Later scheduling, if separately authorized, should use one existing serialized
operations cycle with an archive-only forecast tick **before** collection, one at a
time, and no added provider requests. The tick examines prior passes' immutable
discovery summaries and books; collection then completes/commits discovery before
requesting books. There is no intervening or after-collection witnessing tick. A
later tick can observe a completed discovery phase even when books failed. If its
receipt is after C or its inputs are stale, accept the appropriate abstention;
do not add a witnessing phase or expand freshness simply to recover coverage.
Its budget must come from a remeasured combined wrapper budget; do not add 15 seconds
to an unchanged 90-second deadline and claim the same headroom. Keep collection and
forecast status separate. No second overlapping writer job or unbounded retry loop.
For manual settlement/evaluation batches, use the documented maintenance coordination
in `docs/integration.md`; forecast gaps during stopped collection remain visible.

### Compatibility and rollback (provisional shared interfaces)

| Reader/writer | Schema 1 | Schema 2 | Proposed schema 3 | Unknown future |
|---|---|---|---|---|
| Reference integration code | Supported collection | Supported collection/settlement | Reject | Reject |
| New F3 collector/ops | Preserve legacy behavior | Preserve legacy behavior | Explicit two-phase collection + summary support after tests | Reject |
| New forecast/evaluation | Explicit migration required | Explicit migration required | Supported | Reject |

`forecast migrate` requires schema 2; on schema 1 instruct the operator to perform
the existing explicit settlement migration first. No migration on collection, tick,
health, inspection or replay. `db-init` still creates 1. Update `Store` guards,
settlement `require_schema/migrate` (2 and 3 supported, no downgrading), replay,
operations schema/column/count checks and installed-wheel tests together. Preserve
unknown-schema rejection. This is an additive expansion with no contraction or
automatic historical data backfill. Receipt backfills cannot create historical
availability. Verify populated 1→2→3 and 2→3 on copies.
On schema 3, immutable discovery summaries, publication records and their receipts
must be included in backup/restore/reference checks. Legacy schemas lack the independent
discovery fact: do not synthesize it from a successful book run or relabel old mutable
summaries as newly witnessed pre-C evidence. Diagnostic legacy studies stay distinct.

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
| F3.1 Pure policies | New `forecast/policy.py`, `baselines.py`, `publication.py`, `evaluation/scoring.py`; `tests/forecast/test_policy.py`, `test_baselines.py`, `tests/evaluation/test_scoring.py` | Independent cutoff/publication/settlement decisions, timeline and timing sweeps, exact midpoint/score golden cases; no IO/import side effects |
| F3.2 Expansion | `migrations/003_forecast_evaluation.sql`, new `forecast/schema.py`, `forecast/journal.py`; minimal `storage.py`, `settlement/schema.py`, `ops/archive.py` compatibility | Explicit idempotent migration and transactional rollback on injected DDL failure; old table fingerprints unchanged; 1/2/3 backups and future guards; append-only/uniqueness/JSON reference checks |
| F3.3a Discovery boundary | Future schema-3 path in `collector.py`, storage summary insertion; `tests/test_collector.py`, `tests/test_integration.py` | Bounded discovery/identity/eligibility summary commits before any book; book outcome variants preserve it; resume uses new pass; incomplete discovery fails closed |
| F3.3b Evidence/selection | New `forecast/selection.py`, `forecast/service.py`; `tests/forecast/test_selection.py`, `test_journal.py` | Complete candidates, pre-C receipts for summary and inputs, no future mapping, repeat/crash behavior, no added witnessing phase |
| F3.4 Forecast CLI | New `forecast/cli.py`, dedicated research TOML parser; minimal `cli.py` registration and health filtering; `tests/forecast/test_cli.py` | Bounded tick/reconstruct/inspect, clock/source-mode gates, package entry points, no implicit migration or HTTP |
| F3.5 Outcome/evaluation | New `evaluation/outcomes.py`, `service.py`, `journal.py`, `cli.py`; `tests/evaluation/test_outcomes.py`, `test_service.py` | Two separate views, invariant cutoff population, orthogonal coverage counts, correct latest outcomes, paired denominators, immutable corrections, manifest replay |
| F3.6 Integrated proof | Extend `tests/test_integration.py`, `tests/ops/test_operations.py`, `tests/integration/wheel_smoke.py`; later user/operator docs | Populated migration and full backup/restore/forecast+evaluation replay; preserved collector predicates and legacy-schema behavior plus tested new phase boundary; fresh installed-wheel smoke; separate real forward evidence or explicit inconclusive outcome |

The collector phase boundary **must change in a future implementation**; preserve
its predicate rules and per-book freshness guards while removing book-success
dependence from discovery. No such edit belongs in PR #4 during re-review. Shared
integration-review touchpoints are this discovery interface, schema validation,
core health run kinds, CLI/config, mapping evidence, replay order and operations backup.
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
| T=20:00, C=19:00; attempts 19:00:10/19:01; postponement observed 19:00:30 | Identical cutoff eligibility/probabilities; first scenario may publish, second vetoes; both retained in cutoff population and separate forward coverage (§9) |
| Latest pre-C status Live/Postponed/TBD; missing game in covered schedule | Abstain despite older Preview record |
| Book age 300s vs 300s+1µs | First accepted, second stale; zero/future ages tested |
| Newest qualifying witnessed book is 350 seconds old | Midpoint `book_stale`, no freshness expansion or future-snapshot fallback; constant depends only on common inputs |
| Receipt/publication at C+150s vs +1µs | Timely vs late; before-C preparation alone never counts as publication |
| Same pre-C manifest, changed invocation delay or post-C veto | Cutoff decision and reconstruction unchanged; publication/forward coverage may differ, no mixed reason list |
| Identical discovery sources, receipt and binding; books succeed/fail/timeout/truncate | Common eligibility and constant cutoff coverage unchanged; summary immutable despite partial run; midpoint/paired coverage reflects book result |
| Partial pages, ambiguous membership, missing eligibility decisions, phase interrupted before commit | No eligible complete-discovery assertion; no books before phase commit; constant and midpoint fail closed for affected scope |
| Phase complete, crash before/during books, resumed pass | Existing phase fact survives, new invocation has a new summary; receipt time remains actual, no completion backdating |
| Crash before decision commit / after decision before publication / after allowed publication before receipt | No decision / no published forecast / unconfirmed publication; cutoff facts unchanged once committed |
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

**Timing sweep:** drive a deterministic virtual timeline of the single existing
tick→discovery/eligibility/summary commit→books cycle (nominal starts 120s apart).
Sweep C offsets `{0,1,60,119,120}` seconds relative to cycle start; discovery+book
completion durations `{0,10,60,89,91}` seconds (91 exercises the 90s collector
deadline); next-tick witness delays `{0,1,30,120}` seconds; publication receipt
latencies `{0,10,60,150,150.000001,180}` seconds after C; and one skipped collection
cycle versus none. Model actual serial start/finish times and the configured tick
budget, not overlapping idealized cycles. Include the 350-second book as a fixed
case. A slow/missing pass can make both common and book evidence stale; the oracle
checks each separately. For every combination select only retrieved-and-witnessed
pre-C inputs, enforce age <=300 exactly, and derive publication separately.
Holding the pre-C manifest fixed while varying only publication latency/veto must
leave cutoff eligibility and both reconstructed probabilities bit-for-bit unchanged.
Some one-missed-cycle combinations must abstain; no acceptance criterion requires
all such cases to remain covered. No simulated extra witness or enlarged freshness
is permitted to make the sweep pass.

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
An initial identity binding is committed/witnessed at 18:57:50Z. Fresh schedule and
book are received at 18:58:00Z/18:58:20Z, then witnessed and the binding renewed at
18:58:30Z. The complete discovery summary and every dependency also have pre-C
receipts and qualifying freshness. At C the schedule is Preview,
identity/rules unchanged. Selected lower-ID team's best YES bid=0.4001 and NO
bid=0.5799, both with positive quantities. YES ask=0.4201; midpoint=0.4101.
Decision inserted at 19:00:12Z, allowed publication record committed after its guard,
then publication witnessed at 19:00:13Z: publication delay
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
150 seconds, although still before T. Publication is missed; cutoff eligibility and
qualifying probabilities are unchanged. It is not a timely forward result. A separate
reconstruction created October 3 records that actual date and reconstruction mode;
pre-C raw timestamps alone cannot prove availability. A fixture using those same
timestamps remains synthetic.

**Reviewer's publication timeline:** T=20:00, C=19:00, with the normal example's
same qualifying pre-C manifest in two alternative first-attempt scenarios. Both
baselines are cutoff eligible: constant=0.5, midpoint=0.4101. A postponement is
observed at **19:00:30**. No post-C evidence enters either probability.

| Fact / report field | First attempt at 19:00:10 | First attempt at 19:01:00 |
|---|---|---|
| Cutoff eligibility and reasons | E=1, no cutoff abstention | E=1, no cutoff abstention |
| Reconstructed probability pair | (0.5, 0.4101) | (0.5, 0.4101) |
| Operational knowledge at attempt | Postponement not yet observed | Observed postponement vetoes publication |
| Actual publication | Allowed; assume receipt 19:00:11, delay 11s, `published_timely` | `vetoed`, attempt delay 60s, no publication time; retain postponement raw reference/observed time |
| Cutoff population and coverage | U=K=M=P=1; cutoff paired coverage 1/1 | U=K=M=P=1; cutoff paired coverage 1/1 |
| Forward publication coverage | FK=FM=FP=1; paired publication coverage 1/1 | FK=FM=FP=0; paired publication coverage 0/1; vetoed opportunities=1 |
| Reconstruction N if same contract later has eligible binary settlement | 1 | 1 |
| Forward N with that settlement | 1 | 0, paired scores null/inconclusive |

The later postponement annotates the first scenario but cannot revoke its successful
publication. In the second scenario, it cannot erase the cutoff decision. If final
settlement is unresolved/exceptional instead, **both views** lack a binary score for
that game and show the outcome reason; their original cutoff and publication coverage
remain as above. These two scenarios are not two game samples to pool. If both times
are retries in **one** actual ledger, the successful 19:00:10 publication remains the
single original result; the 19:01 retry returns it and appends no replacement or
retroactive veto. The warning is retained separately.

**Reviewer's 350-second-old book:** with C=19:00:00, the newest qualifying book was
retrieved at 18:54:10 and witnessed before C; age=350s. Midpoint abstains `book_stale`.
Even a book retrieved 18:59:00 but first witnessed 19:00:01 cannot rescue it. If
discovery/identity/schedule inputs separately qualify, constant remains 0.5, K=1,
M=P=0 for U=1; otherwise record those independent common exclusions too. Keep 300s
and the same tick ordering. Do not add a witness between collection passes to obtain
a nicer result. The timing sweep includes one missed cycle without promising coverage.

**Discovery unchanged, books varied:** freeze one complete discovery-phase summary
and its pre-C receipt/binding, giving U=E=K=1. In case A the selected book succeeds
and qualifies, so M=P=1. In case B only that book attempt times out or returns malformed
data, so M=P=0 with a book reason; even if the whole collection run is partial, E=K=1
and the immutable discovery summary/digest remain identical. Common eligibility and
constant cutoff coverage are independent of book success. Missing/ambiguous discovery
instead would fail the common gate in both cases, not get repaired by a successful book.

**Corrections and exceptions:** evaluation E1 uses finalized YES revision 1 for
game 900001. A later finalized NO revision 2 yields E2 with `supersedes=E1`, the
same forecasts and y=0 (midpoint Brier=0.16818201). E1 is unchanged. A subsequent
scalar 0.5300 revision yields E3 with exceptional exclusion and no binary y; paired
scored N becomes zero with one exceptional outcome. An MLB winner does not fill it.

## 10. Design record, hazards and remaining decisions

Load assumption (estimate): 30 games/day × 2 baselines = 60 forecast outputs/day;
at roughly 8 KB per complete game decision+manifest, about 0.24 MB/day (~88 MB/year),
plus bindings, publication records, discovery summaries, evaluation versions and receipts.
One publication row/game adds roughly 30 rows/day; at 120-second cadence there are
at most 720 scheduled discovery summaries/day before manual invocations, each bounded
by the existing discovery caps/manifest-size limit. Measure their actual payload size
rather than treating the decision-only 0.24 MB/day estimate as total growth.
Receipt volume follows ingestion,
not game count: at 30 books/120 seconds, 21,600 books/day × estimated 250 bytes/receipt
≈5.4 MB/day before indexes and other retrievals. Measure actual growth. Retain evidence
in v1; no automatic pruning. Main archive scans, not scoring arithmetic, constrain
10× growth. Index/cursor improvements and archived evaluation copies precede any
new server. One local SQLite database, one writer, zero extra network QPS suffice.

Local responsiveness target (not measured): a 100-game tick p95 <=5s and p99 <=10s
on this Mac after bounded indexed selection; hard budget 15s. Report measured
distribution later; do not average percentiles. Missed publication deadlines reduce
forward publication coverage; missing pre-C receipts can independently reduce cutoff
coverage. A late computation alone does not alter qualifying reconstruction coverage.
No availability SLA; integrity takes precedence over an on-time forecast. Committed
rows retain SQLite FULL durability assumptions; host/disk loss is outside local
redundancy. RPO/RTO depend on verified backups and are unmeasured.

| Choice | Rejected alternative and reason |
|---|---|
| Existing SQLite + immutable records | A server/queue adds operations at ~60 outputs/day without improving temporal truth |
| Conservative availability receipts | HTTP timestamps/sequence masquerading as commit or mapping availability permits leakage |
| Independent pre-book discovery summary | End-of-run completion conflates book faults with candidate/constant eligibility |
| Separate cutoff, publication and outcome facts | A single eligibility flag lets post-C warnings or invocation timing select the research sample |
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
| H-14/18 retry and unknown completion | Deterministic keys, atomic cutoff decision, separate publication verdict/witness; unknown stays unknown, never backdated; pre-book summary survives later book failure |
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
4. **Measured coverage:** 300s freshness and 150s publication grace are fixed v1 policy,
   not validated optimal parameters or missed-cycle guarantees. Measure cutoff and
   publication coverage separately. Accept stale abstentions with the current tick
   order; no freshness expansion or new witnessing phase without demonstrated need
   and separately reviewed protocol change.
5. **Production scheduling:** not selected or authorized here. Default: finite commands
   and controlled forward demonstration first; later remeasure a single serialized
   schedule. Existing collector service status must be inspected anew at deployment.

Skill provenance: `planner`, `system-design`, `data-systems-design`, `detail-planning`,
Cool Coder engineering-skills **1.2.0**, pin
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. The 24 files in these installed folders
matched the pinned upstream archive on a read-only comparison. No installations
were changed. The user's requested document paths take precedence over the skills'
default plan/executor persistence paths. This specification stops at milestone 3.
For this review revision, applied `planner` and `data-systems-design` at that same
pin, read-only. The three review decisions above are resolved; remaining integration,
legacy-data and operational measurement items are limitations, not alternative rules
that may override the separated eligibility/coverage contracts.
