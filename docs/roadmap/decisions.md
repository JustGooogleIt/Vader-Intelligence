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
