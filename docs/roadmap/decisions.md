# Decision log (roadmap run, from 2026-10-18)

Each entry: context, decision, alternatives, reversibility. Material decisions about
evidence meaning, financial behaviour or historical compatibility use a conservative
default and keep alternatives behind explicit configuration.

## D-001 Record contract 2 was amended in place; introduce contract 3 (2026-10-18)

Context. PR #12 (`27a3473`) changed the validity rules of contract-2 receipts (writer
must be a `forecast-witness` run whose provenance names namespace and session), the
derivation of typed-reference `source_mode` for two-phase runs (persisted/inferred
lineage), and evaluation-item scoring (scored items need a pinned decision), without a
version change. Verified facts: PR #10/#11 witness runs (`841af2a`, `5811991`) never
recorded `tool_configuration.observer_namespace/session_id`, so every contract-2
receipt they wrote fails the amended integrity rule; typed references they embedded
can disagree with the amended `source_mode` derivation. The same label "contract 2"
therefore names two incompatible rule sets that both wrote real files (the PR #10
reviewer's schema-4 wheel archive; the PR #16 shadow smoke archive).

Decision. Compatibility is not provable, so version explicitly:
- Contract 3 (receipts, decisions) carries the amended rules at the storage level.
  Schema 5 (`forecast migrate --record-contract 3`, no DDL) opens it and closes
  contracts 1 and 2 for new receipts/decisions.
- Contract 2 is frozen as "written by draft code between PR #10 and PR #16": storage
  validity is the union of the two writer rule sets, so no archive written by either
  is bricked; every row is classified `current` (valid under the amended rules) or
  `legacy_draft` (valid only under the PR #10/#11 rules). Rows are never converted.
- Operational proof is one predicate for every reader: a contract-2 receipt counts only
  when it satisfies the amended operational-witness rule (true of PR #12–#16 witness
  receipts, never of draft-era or supplied ones); `legacy_draft` rows are never proof.
- New evaluations score contract-3 decisions; a legacy contract-2 cohort is evaluated
  only on explicit request, excluding draft-classified decisions and decisions computed
  before renewal at C existed.
- Evaluation items: the PR #12 scored-item pin rule is a current storage rule for every
  new item; an existing item valid only without it is `legacy_draft` and its evaluation
  is listed as legacy.

Alternatives rejected: (a) keep the amendment and assume no schema-4 archive exists
(explicitly disallowed); (b) one contract-2 meaning chosen after the fact (either
bricks draft archives or re-admits the review's P1 receipts); (c) refuse every contract-2
receipt as proof (discards genuine PR #12–#16 witness observations without cause).

Implementation facts (PR `fix/record-contract-3`):
- Verified on archives the older code itself wrote (`tests/fixtures/legacy_archives`):
  under PR #16 code the PR #11 archive failed integrity ("reference provenance
  mismatch") while the projection admitted its 9 `forward_shadow` receipts for synthetic
  fetches (review P1 reachable through legacy data): the reader predicate (run kind only)
  was weaker than the storage rule (run kind + namespace/session provenance).
- One shared operational-proof predicate (`evidence.PROOF`) now serves projection, witness
  deduplication, tick and evaluator: contract 2 or 3, namespace, and a forecast-witness run
  whose provenance names namespace, session and source lineage. Draft-era witness runs never
  recorded those, so none of their receipts is proof; PR #12–#16 receipts still are, and are
  not re-witnessed after migration.
- Integrity classifies every row `current` or `legacy_draft` (valid only under the frozen
  draft rules: typed-reference derivation of 5811991, no witness-run rule, no scored-item
  pin rule) and reports the current-rule reason. A contract-3 row has no legacy rule set.
  Consequence for malformed archives: a supplied contract-2 receipt inserted around the
  journal is reported as a legacy row with its reason (it was valid under the draft
  rules), not as an integrity failure; it is still never proof. A supplied contract-3 row
  is an integrity failure.
- Evaluations cover one record contract (default 3); `--record-contract 2` evaluates a
  legacy cohort and excludes draft-classified decisions and decisions without renewal at C
  (`pre_renewal_semantics`), listing them. Reports show the contract; `compare` refuses
  evaluations of different contracts (including pre-D-001 evaluations, which record none).
- Schema-4 archives are read-only for research under the new code until migrated (witness,
  tick, reconstruct and evaluate require schema 5); collection is unaffected. Old binaries
  (main, PR #16) refuse schema 5 without mutation.

Reversibility: additive; migration 005 has no DDL and touches no row.

## D-002 Time-less postseason rules: eligibility/mapping rule v2 (2026-10-18)

Context. From about 2026-10-09 Kalshi `KXMLBGAME` rules read "...game originally
scheduled for Oct 19, 2026, then..." with no time. Rule v1 requires the stated time to
equal the official start. Production archive (read-only query, 30 events): every one of
38 timed rules has an event-ticker time code (`KXMLBGAME-26OCT192008CLETB` → 20:08 ET)
equal to the rule-stated time; 22 rules are time-less.

Decision (pending implementation in A2). Rule v2 accepts the time-less form only when
(date, exact participants, yes team) identify exactly one official game; the start
comes only from the fresh official MLB schedule; the ticker time code is versioned
corroboration that must equal the official start in America/New_York (mismatch or
absence fails closed). Close/expiration/occurrence times are never used. Enabled only by
explicit collector configuration, which changes the collector configuration hash, so v2
evidence forms its own cohort.

Implementation facts (PR `feat/eligibility-rule-v2`):
- `scope.RULE_V2` + `_eligible_v2`; rule v1's code path is unchanged. Ticker
  interpretation `ticker-v1` (`scope.ticker_identity`): date, America/New_York time, team
  codes from the reviewed upper-case aliases, away first; a game number suffix is refused
  under v2. New reasons: identity `ticker_unparsed`, `ticker_identity_mismatch`,
  `home_away_reversed`; pregame `ticker_start_mismatch` (forecast renewal and the
  publication veto classify them). Identity requires exactly one official game between the
  two teams on the date in either order; doubleheaders and reschedule markers fail closed.
- `Config.eligibility_rules` (default `("rule-v1",)`); `Config.provenance()` omits the
  default, so the default cohort hash equals the earlier code's and v2 runs hash
  differently. Discovery manifests record `policy.eligibility_rules`.
- Settlement mapping policy 2 (`mapping._resolve_v2`): the same identity proof, plus a
  provider `rescheduledFromDate` link for a postponed game; no prior-mapping reuse. The
  rule set comes from the settlement run's recorded configuration, so replay re-derives
  the same mapping without any caller configuration.
- Real archived evidence (read-only recheck of production's archived event and schedule
  bytes at the original check times, `tests/integration/rule_v2_archive_recheck.py`,
  output `docs/evidence/rule-v2-archive-recheck-20261019.json`): 650 sampled checks of 26
  tickers; v1 alone 100 eligible (timed rules) / 550 unrecognized; v1+v2 488 eligible, 104
  `ticker_start_mismatch` (official times that moved, e.g. `26OCT122000LADMIL`), 38
  `not_scheduled_pregame`, 12 `unmapped_game` (the postponed `26OCT161800TBCLE`), 8
  `start_time_unknown`; every v2 start equals its ticker time; timed-rule decisions are
  identical with v2 enabled. This is a recheck of stored bytes, not forward evidence.
- Alternative kept in reserve: Kalshi's public `/milestones` reports a `start_date` that
  matched the ticker time in the docs research; it would be a new collected source with
  its own retrieval semantics, not needed while the ticker corroborates.

## D-003 Schedule revisions: explicit abstention before C, unscored disposition after C (2026-10-19)

Context. The previous release deferred a schedule-revision state machine (binding
revision chains, a moving C) and abstained with `schedule_revised`. Real archived evidence
shows revisions are common (official times moved for `26OCT122000LADMIL` and
`26OCT152000MILLAD`; `26OCT161800TBCLE` was postponed). The evaluator meanwhile scored a
decision whose game the settlement mapping showed at another start
(`evidenced_schedule_change`), i.e. a forecast whose real horizon was not 3600 s.

Decision.
- No binding revision chain or moving C: under rule v1 the rule-stated time, and under
  rule v2 the ticker time, must equal the official start, so a revised start is
  ineligible by definition. A chain could only ever produce abstentions. The binding's
  first C stays sticky; the decision at C abstains with the revision reason
  (`schedule_revised`, `schedule_rule_time_mismatch`, `ticker_start_mismatch`,
  `rescheduled_game`, `unmapped_game`) from the pass applicable at C. A revision that is
  reverted before C, as seen by the applicable pass, leaves the decision eligible.
  Revisions not witnessed by C do not apply at C.
- After C: the decision stands; publication is vetoed by already-available evidence that
  the game is no longer pregame (existing). If the settlement mapping confirms the same
  game at a different start, the evaluation disposition is
  `schedule_changed_after_decision` (reason `start_moved:<mapping reason>`, or
  `mapping_start_unknown`), never scored. Evaluator version `evaluation-v2` (it also
  covers D-001's one-contract scoping).

Alternative (configurable later if wanted): score such opportunities in a separately
labelled bucket as long-horizon forecasts of the same contract. Not done: it changes the
meaning of a T-60 score.

## D-004 Registered forecasters in the forward path: decision contract 4 (2026-10-19)

Context. Forward-shadow decisions (contracts 1–3) carry exactly the baseline pair. A
candidate model can only be shadow-tested if the tick computes its forecast at C from the
same witnessed evidence, under the same publication and evaluation semantics.

Decision (to implement after the B/C/D/E/F/G cores are stacked): a new record contract,
not an amendment.
- Decision contract 4 = contract 3 fields plus `forecasters` (sorted list of
  `{id: "model:<sha256[:16]>", artifact_sha256, family, feature_set}`); `results` keys are
  the baseline pair plus those ids. Only forward-capable feature sets are admitted.
- The research protocol lists artifact digests (they enter the protocol hash, so a
  different model set is a different cohort); the registry path is operational only.
- At C the tick builds quote series only from fetches witnessed by C (the projection's
  rule), computes the features, runs the artifact; any missing/incompatible artifact or
  unavailable `forbid` feature is an explicit abstention reason, never a fallback.
- Evaluation item contract 2 scores every forecaster of the cohort on the same population
  with the same publication and outcome dispositions; reports pair each with midpoint.
- Opened by an explicit no-DDL migration (schema 6), following D-001's pattern.

## D-005 NBA and NFL adapters: blocked on an authoritative schedule source (2026-10-19)

Context. A5 asks for NBA/NFL adapters only if they can reuse the architecture with
league-specific identity, schedule and settlement semantics. Research (16 public GETs and
documentation, `docs/evidence/nba-nfl-sources-20261019.md`): Kalshi is ready
(`KXNBAGAME` 1,516 settled games, `KXNFLGAME` 474; two complementary contracts per game;
NFL ties settle at $0.50 each; date-only tickers and rules, no start time). The blocker is
the official schedule/result source the MLB vertical relies on: the NBA CDN schedule
returns 403 to scripts and the NBA terms restrict regularly updated databases; the NFL has
no free official API; ESPN's public endpoints work but are unofficial and their terms
forbid automated access.

Decision. A5 is blocked by a data-source authorization decision, not by engineering. No
adapter is built on a source whose terms forbid this use. If the user accepts ESPN (the
second-ranked settlement source in both Kalshi contracts) or provides an authorized source,
the recommended order is NFL first (research only; nflverse as an independent cross-check;
ties and fair-price resolutions as explicit non-binary outcomes), then NBA (preseason,
makeup games with new tickers, NBA Cup final outside the series).

## D-006 Execution layer: separate ledgers, unverified fees, conservative defaults (2026-10-19)

Context. D/E need paper execution, risk and sizing that a later live path can share
without letting simulation leak into live state.

Decision (#24, spec `docs/specs/execution-risk-v1.md`). Decimal money only. Paper and
live ledgers are separate files whose environment is fixed at creation and enforced by
triggers; events are append-only. The executor writes `submit_requested` durably before
any gateway call and records an unanswered submit as `submit_unknown`, resolved only by
reconciliation, never by a blind retry. The risk engine is authoritative (limits,
reservations, kill switch, unknown-state refusal); fractional Kelly sizes within it.
Fee schedules are versioned and marked unverified; the higher-fee schedule is the
default. The simulator defaults to worst-of-bracketing books, a 50% depth haircut, no
maker fills and latency; depth-unknown snapshots (quotes, candles) supply no depth unless
the labelled `assumed_depth` scenario is chosen.

## D-007 Live adapter implemented, disabled by default (2026-10-19)

Decision (#25, spec `docs/specs/live-adapter-v1.md`). A live gateway exists only after
`activate_live` passes every gate (acknowledged config hash, enabled, environment,
key id, secret reference without key material, caps, market rules, live and consistent
ledger, complete limits matching their recorded hash, explicit kill-switch disengage),
with the credential read last. No CLI path activates live trading; `vader live
readiness` checks every local gate without the key or the network. The canary runbook was
not executed. Activation remains a separate user decision after review.

## D-008 Research pods: approved operations, staged inputs, recomputed evidence (2026-10-19)

Context. G asks for autonomous research pods without letting generated work reach
credentials, production data, orchestration state or the final holdout.

Decision (#27, spec `docs/specs/research-orchestration-v1.md` §8).
- Only registry operations with typed parameters run; no agent-generated code executes.
  Workers are local subprocesses with a scrubbed environment and resource limits; this is
  fault and resource isolation, explicitly not a security sandbox.
- Inputs are staged by the coordinator as verified read-only copies; a dataset snapshot
  holds development and validation rows only and refuses any row outside its segment.
- Pod outputs are proposals and diagnostics. The protected evaluator recomputes every
  promotion metric from the registry. Default `validated` criteria: log loss vs market mid
  on paired validation rows, N ≥ 100, paired bootstrap 95% CI entirely below zero, and a
  negative estimate in each chronological half; the number of proposals on the snapshot
  is recorded. Shadow evidence comes from a stored contract-4 evaluation, paper evidence
  only from a coordinator-run backtest plus an operator approval. No lifecycle state names
  live eligibility.
- Search budgets are declared parameters (a retried task reproduces its artifact digest).

## D-009 The 2026 final holdout stays untouched (2026-10-19)

Context. The roadmap asks for a one-time holdout evaluation of the final candidate. On
the real 2026 dataset every model, from direct training (#21) and from research pods
(#27), is worse than market mid on validation; the evaluator rejected all of them.

Decision. No holdout is consumed. Evaluating a rejected candidate on the holdout would
spend the only unbiased test on a model already known to lose to the market, and the
lifecycle refuses it anyway (holdout only from `validated`). The holdout (official dates
after 2026-09-01 under `dev<=2026-07-31|val<=2026-08-31|embargo=1`) is reported
untouched in both ledgers. The one-time holdout path is demonstrated end to end on
synthetic data (`tests/integration/e2e_demo.py`). Open question for the user: whether a
two-season dataset (2025 + 2026) should define a new split and holdout before any
further model work.

## D-010 Historical backtests execute on candle quotes; depth is a labelled scenario (2026-10-19)

Decision (#26, spec `docs/specs/history-backtest-and-dry-run-v1.md`). Backtests use candle
closing bid/ask within [C − 120 s, C + 300 s] (feature stage merged with the
execution-only post-cutoff stage); candles carry no sizes, so nothing fills unless an
`assumed_depth` scenario is chosen, and every report says so. Evidence for the price
assumption: on the 12 postseason games present in both archives, candle mid and spread at
C equal those of the books the production collector retrieved before C
(`docs/evidence/real-data-demo-20261019.md` §2); depth at arrival remains unverified.

## D-011 Live-adapter dry run clips sizes to the limits (2026-10-19)

Decision (#26). `vader live dry-run` sizes with the paper risk engine and clips to the
limits instead of refusing: a canary sizes down, and a refused order would hide whether
the request itself is well formed. The dry run writes only a paper ledger, signs only with
an in-memory throwaway key on request, and has no HTTP client.

## D-012 Reuse parsed committed ledger rows; keep the projection a full recomputation (2026-10-19)

Context. Paper runs cost time quadratic in the input count. The profile of a real 401-row
backtest put 185 of 207 seconds in `Ledger.projection()`, almost all of it re-reading
and re-parsing rows (#28 evidence §3).

Decision (#29). Full reads of the eight append-only record tables reuse rows parsed
outside any transaction, and read only rows with a larger rowid. This is sound because
triggers refuse UPDATE and DELETE and one writer commits at a time. Rows read inside a
transaction are never kept, and `verify()` recomputes from disk. The projection itself
stays a full fold over the records, now about 15 of 42 profiled seconds. An incremental
projection would change how state is derived in the authoritative ledger; it is left as
a separately reviewed follow-up. Result on the real scenario: the same report, with CPU
time down from 78 s to 16 s. At season scale (2,027 forecasts, 547 fills) the report is
again identical, and wall time falls from about 21 min to 4.3 min.

## D-013 Pod autonomy: planners propose approved operations from development scores (2026-10-19)

Context. G asks for autonomous research pods. Until #30, a pod ran only what an operator
submitted. The security boundary (D-008) allows only declarative, approved operations,
because worker processes are not a security sandbox.

Decision (#30, spec §9).

- **The planner.** A planner chooses the next operations from a development-only
  context: the pod's own tasks, with allowlisted fields such as development
  cross-validation or inner-split scores and the selected configuration. Validation
  diagnostics, candidates and the holdout are excluded, so adaptive planning cannot
  overfit the validation rows.
- **Proposals.** Proposals pass the same typed contracts as operator submissions;
  nothing a planner returns is executed as code. A provider planner (LLM) exists behind
  the `ModelProvider` interface. It runs only against the scripted double; a real vendor
  raises `PaidCallsDisabled`.
- **The loop.** The loop is bounded by rounds, a total task budget per pod and snapshot,
  and wall time; the bounds are checked before submission. It records each round as a
  chained artifact *before* submitting the round's tasks, and resumes from the store
  after a crash. Malformed provider output is a recorded rejection, never an exception
  (fixes from an independent review, #30 `d744766`).
- **Evaluation.** One candidate per pod (the best by development score) goes to the
  protected evaluator, which recomputes the validation metrics and records the
  snapshot's proposal count.

## D-014 A dataset's final holdout is pinned once in the registry (2026-10-19)

Context. An independent pre-review of #27 showed that the holdout was defined only by
the split an operator typed. The holdout ledger was keyed by the full split key, so a
later split could move final-holdout rows into validation, and a pod could score them,
while the ledger still reported the holdout untouched. The real registry was never
affected: it had no holdout entries, and every evaluation used validation ≤ 2026-08-31.
Nothing enforced that, though.

Decision (#31).
- **The pin.** Each dataset's final holdout start is recorded once in an append-only
  table (`holdout_pins`). It is set by `vader dataset pin-holdout`, or at the first
  writable training or snapshot use.
- **What it enforces.**
  - Every split must end before the pin.
  - A holdout evaluation must hold out exactly from the pin, and its ledger entry is
    keyed by it, so every evaluation of the same rows shares one key.
  - Pod snapshots (`vi-pod-dataset-v2`) carry the pin, and every reader refuses rows on
    or after it.
- **The real datasets.** The three real datasets that have models are pinned at
  2026-09-02, the D-009 boundary, by operator action recorded in the registry.

## D-015 `last_trade_gap_c` is not the same feature in history and forward (open, 2026-10-19)

Context. An independent pre-review of #21 found that `last_trade_gap_c` in `fs-market-v1`
has two meanings:
- **From candles (history):** a trade price exists only in minutes that had trades, so
  the gap is the age of the last trade.
- **From live listings (forward):** `last_price_dollars` is the last trade ever, stamped
  with the listing's retrieval time, so the gap measures the retrieval's age.

The same quiet market therefore gives a value forward and `None` historically. Models
trained on history see a different input distribution forward.

Decision. Nothing is changed in this run. Changing the feature would change
`fs-market-v1` under trained models and the running shadow. Instead:
- tonight's forward shadow results for fs-market-v1 models carry this caveat;
- the proposal is `fs-market-v2`, which either drops the feature or computes it only
  from a witnessed trade time.

This is open for the user, together with the next feature work.

## D-016 The midpoint baseline at C abstains under the specified cycle order (open, 2026-10-19)

Context. In both live forward shadows on 2026-10-19 the midpoint baseline abstained
with `book_availability_after_cutoff` at C = 23:08:00Z
(`docs/evidence/live-forward-20261019.md`). The cause is the order the specification
fixes, not a fault:
- Each cycle runs the tick first. The tick witnesses evidence from earlier cycles, then
  the collector runs. The F3.3 plan, §4.2 and §6, says: "The tick runs before
  collection… No extra witnessing phase is added."
- Book selection (`forecast/selection.py:select_inputs`) anchors on the latest book
  retrieved by C. If that book was not witnessed by C, it abstains; it never falls back
  to an older book.
- The latest book retrieved by C is therefore witnessed by the next cycle's tick,
  which runs after C. The only exception is a cycle that starts in the few seconds
  between its predecessor's book retrieval and C (tonight about 4.5 s, so about 4% of
  phases at a 120 s cadence).

The plan's timing table covers discovery passes, which may fall back to an older
witnessed pass. It states that "book evidence follows the same arithmetic". It does not,
because books have no fallback.

Tonight's timing: the book was retrieved at 23:06:15.8 and witnessed at 23:08:12.2. The
previous book was retrieved at 23:04:15 and witnessed at 23:06:11; under a fallback
rule it would have passed the availability and freshness checks (age 224 s ≤ 300). The
registered models were unaffected: their listing quotes come from the latest market
fetch witnessed by C (retrieved 23:04:15, witnessed 23:06:11). No forecaster used
anything witnessed after C.

Effect. Forward evaluations usually have no market-midpoint score, so a registered
model can be compared only with the constant baseline. Forward evidence is meant to
make exactly that market comparison.

Decision. Nothing is changed in this run. Both rules are established contracts, and the
F3.3 plan states the first explicitly. Options for the user:
1. **Anchor book selection on availability** (recommended). Use the latest book
   attempt that was both retrieved and witnessed by C. If that attempt is unusable,
   abstain, so outcomes still cannot select a book. This matches the discovery pass
   rule and the registered models' quote rule. It changes decisions, so it needs a new
   selection version (for example `selection-v2`); stored decisions keep their own
   version.
2. **Add a witness pass after collection** in the shadow cycle. This reverses the
   plan's "no extra witnessing phase" rule and adds a budget to the cycle (tick +
   deadline + witness + three grace periods below the cadence).
3. **Keep both rules.** Bounded drivers can still get a midpoint by placing one extra
   ordinary cycle about 1 s before C: its tick witnesses the previous book before C,
   and its own book is retrieved after C. This uses existing code only, but it depends
   on the book taking more than 1 s to arrive, and a launchd cadence cannot do it.

Reversibility. Options 1 and 2 are additive behind a new version or configuration.
Option 3 changes nothing.
