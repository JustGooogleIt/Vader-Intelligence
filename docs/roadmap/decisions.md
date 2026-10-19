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

## D-004 Registered forecasters in the forward path: decision contract 4 (2026-10-19, planned)

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
