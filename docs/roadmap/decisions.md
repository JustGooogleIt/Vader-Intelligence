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
  is bricked; every row is classified (`draft-pr10` / `amended-pr12`).
- Under the current reader, contract-2 rows are never operational availability proof
  and contract-2 decisions are never scored in new evaluations; they stay inspectable,
  replayable and backed up byte-for-byte.
- Evaluation items: the scored-item pin rule is a storage rule only for items citing
  contract-3 decisions; legacy items keep their original rules and are labelled.

Alternatives rejected: (a) keep the amendment and assume no schema-4 archive exists
(explicitly disallowed); (b) reinterpret draft receipts as proof when they happen to
satisfy the amended predicate (changes their meaning and still leaves label ambiguity).

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
