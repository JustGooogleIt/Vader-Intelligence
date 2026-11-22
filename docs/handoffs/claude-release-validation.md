# Claude release validation — 2026-10-23

Bounded validation of the integration checkpoint, assigned in
[`claude-next-20261022.md`](claude-next-20261022.md). Codex owns integration and merge
sequencing; this document records independent verification, one remaining-finding
repair (N-34a), one narrowed false positive (F51), the F50 analysis, the decision table
and the next bounded shadow recipe. Nothing was merged, deployed, migrated, activated or
sent to a venue; no holdout was consumed; no provider or paid call was made.

## Checkpoint, branch and environment

| Item | Value |
|---|---|
| Integration branch head at start (fetched 2026-10-23 04:4xZ, as in the handoff) | `fix/integration-readiness-20261022` = `1f62fcb9e9a01759c2a278baeb4eac3494f5366a` (this branch's base) |
| Integration branch advanced during this session | `72a86ae1bfe2526589310af3caa46a5f30a445c5` (2026-10-23 04:56Z, docs only: `plan.md`, `docs/roadmap/backlog.md`, `docs/roadmap/dashboard-and-game-farms.md`, `History/…`); no file overlaps this branch, no code change; nothing here depends on it |
| Contains consolidated repair code | `3b0ed7c7de90daf95e801410eaeffeb5e9a6470f` (yes, ancestor) |
| Contains author candidate (#37 head) | `757c73f986b4481beb2fbddbb596e58450fe2299` (yes, ancestor) |
| This branch | `fix/claude-release-validation-20261022`, new worktree, private `.venv` (Python 3.11.6, uv 0.12.19, `uv sync --locked` at 04:47:25Z) |
| Recovery refs inspected, not merged | `recovery/grok-h-20261022` (`0572248…`), `recovery/f50-20261022` (`37c7bac…`), `recovery/history-tier-20261022` (`be6f71f…`) |
| Commits on this branch | N-34a `500c993c4caa4f96a40b773cab804cd228326fa3`; F51 narrowing + probes `9495817f68dc0d5dd69baff83adee09f1c2747d5`; this document and the findings tracker in the following commit; **2026-10-23 follow-up:** acknowledgement evidence scope (PR #39 review P2) `f24af41be9e02f6453c495b7ab2986347eba32ab`, docs `b08d27cbad0ebced276f67cf567ceeddde6d6f09`; after the internal review: `553fa99732e9d49caa7cf5be6541b2b1f2530435` (code, tests, runbook), spec + this record in the next commit |
| Draft PR base | `fix/integration-readiness-20261022` |

Skills: Cool Coder engineering skills 1.2.0 at `e46e79805be0ee5877fa9bc993492064bbb40aa5`
in `~/.codex/skills`; all 50 files of `docs/checkpoint-skill-provenance.json` match their
pinned hashes (re-hashed this session). Used: `verify` (evidence rule, failure-mode
coverage), `code-review` (`/review-diff` criteria for the repair delta and this branch),
`implement` (conventions for the N-34a change), `reverse-branching` (reverse path below).
No shared skill file was changed. The `~/.claude/skills` directory holds no copy of the
manifest files and was not used.

Local inventory, read-only and left intact: uncommitted F47 draft in
`Vader-Intelligence-wt-grok-h` (`ops/common.py` + four configure cases); uncommitted
N-34a settlement tests in `Vader-Intelligence-wt-grok-i` (two revision cases, one
foreign control). Both worktrees are unchanged. Their intent is accounted for below;
nothing equivalent was duplicated and their evidence is retained.

## Task A — independent verification of the five repairs

Method: read the repair diff `3b0ed7c` in full (source and tests); re-ran its new
regression cases against the unrepaired candidate `757c73f` in a detached scratch
worktree (`PYTHONPATH` to that source); wrote independent probes
(`tests/test_release_validation.py`, 16 cases) for the negative scenarios the handoff
names; ran everything with disposable databases and synthetic keys only.

Failing-before evidence (04:53:03Z, unrepaired `757c73f`, the repair's own regression
groups): **15 failed / 8 passed, 9 subtests passed**. The 15 failures are exactly the
F47, F49 (×2), F48, F51 (×2), F39 `RuntimeError`, registry-unavailable (×2) and
N-33b (×6) cases. At the repaired head the focused set (the handoff's six files) gives
**155 passed, 29 subtests passed in 48.57 s** (04:49:40–04:50:29Z).

| Finding | Independent result | Evidence |
|---|---|---|
| F39 | **Cleared.** A `RuntimeError` at summary commit follows the failed-run path: run status `failed`, `discovery_summary_not_committed` in errors, `books_this_pass` 0, no orderbook request, raw fetch evidence retained. | `collector.py:411-422`; probes `test_summary_runtime_error_keeps_raw_evidence_and_requests_no_books` |
| F39 limitation | **Stated.** If disk pressure also refuses the failure record (`finish_run` raises), the error propagates out of `run()` and the run row stays `running`; still no book is requested and raw evidence is kept. Health shows a running run until an operator inspects. | probe `test_summary_failure_record_that_cannot_be_written_propagates_without_books` |
| F47 | **Cleared.** `configure` validates the shadow before `state.mkdir` / `write_json` (`ops/manage.py:118-130`); `check_shadow_forecasters` builds the tick's own `runner_from_registry` and refuses any model with a constructor error. Unknown set, research-only set, feature-name mismatch and an unloadable (verification-failing) artifact are all refused; the registry files are byte-identical afterwards; a genuinely loadable logistic artifact configures (`tests/ops` good case). | `ops/common.py:180-187`; ported grok-h cases as `test_configure_refuses_every_artifact_the_tick_would_refuse_or_abstain_on` |
| F48 | **Cleared.** Legacy `deposit-<ts>` / `note=init` is recognised; an equal retry adds no cash and keeps the original identity; a different amount, and two legacy initializations, raise `initial deposit conflicts …` without adding cash. Amounts compare as `Decimal` (`CashEvent.__post_init__`), so `1000` and `1000.00` are the same deposit. | `cli.py:1098-1113`; probes `test_an_equal_init_spelled_differently_is_the_same_deposit`, `test_ambiguous_duplicate_legacy_initializations_are_refused_without_cash` |
| F49 | **Cleared.** A missing, uninitialised, corrupt or wrong-schema registry gives an unavailable model (`model_unavailable:` abstention) while `constant-v1` still records 0.5; no `research.sqlite3` is created and an existing file is unchanged; unknown/non-forward metadata still fails at configure; the ops test removes the registry and `stop`/`uninstall` still load the configuration. | `forecasters.py:277-287`; `tests/forecast::test_unavailable_registry_does_not_suppress_baselines`; probe `test_an_unusable_registry_file_is_an_explicit_unavailable_model` |
| F51 | **Cleared with one narrowed false positive (below).** Headerless and middle-body RSA PKCS8/PKCS1 fragments are refused; a headerless Ed25519 PKCS8 body is still caught by the existing structure detector; complete SubjectPublicKeyInfo and PKCS1 public-key bodies and ordinary configuration remain usable; the worst 64 KiB input (500 two-line groups) takes well under 2 s. The documented short/custom/JWK limitations stand. | `activation.py:_wrapped_secret_body`; probes in `tests/test_release_validation.py` |

**F51 false positive (P3, fixed on this branch).** Before the narrowing, two consecutive
bare SHA-256 digests (a TOML array or two comment lines) and two consecutive bare
absolute paths of 60–76 characters were refused as a wrapped key body: hex digits and
`/` are in the base64 alphabet, the joined text decodes, and it is not a public key.
Reproduced at the checkpoint (05:00Z: `test_consecutive_hex_digests_are_not_a_wrapped_key_body`
and `test_consecutive_absolute_paths_are_not_a_wrapped_key_body` failed). Fix: a run made
only of hex digits, or whose every line starts with `/`, is not PEM-shaped and is left to
the existing hex and structure detectors. A real PEM body line is hex-only or
slash-initial with negligible probability, and only a group where *every* line has that
shape is skipped. This narrows a refusal; it does not expand the guarantee, and the spec
and runbook say so.

**Verdict for Task A.** Independent review finds **no material defect in the five
repairs at code `3b0ed7c7de90daf95e801410eaeffeb5e9a6470f`**. The only change made to
their code is the F51 false-positive narrowing above. This clears the scoped repairs
only; it is not approval of the 73k-line stack.

## Task B — N-34a (fixed here)

Reproduced at the checkpoint with the recovered `wt-grok-i` draft applied to this branch
(04:53Z): **2 failed, 24 passed**; both YES→NO cases reported only `balance_mismatch`
(the foreign control passed). Cause: `_reconcile_settlements` skipped every acknowledged
token before looking at the record, so a venue revision of a market the ledger had
settled was never compared.

Change (`trading/executor.py`, `trading/gateway.py`, `trading/kalshi/gateway.py`):

- An acknowledgement covers the venue's records of a market only while the ledger has
  **not** settled it (foreign, unmappable, or awaiting the operator's own settlement).
- Once the ledger has settled the market, each read compares the venue's record with the
  ledger's current settlement on result and value. For `conflicting_records` the gateway
  now carries the distinct parsed records (`UnreadableSettlement.records`), and any one
  of them agreeing is enough (that is what the operator resolved). Otherwise
  `settlement_conflict` is reported, the cursor is kept and risk-increasing orders stay
  blocked until an immutable operator correction (`supersedes_id`) reconciles it.
- Foreign-record handling is unchanged (control test passes).

Tests (`tests/trading/kalshi/test_kalshi_settlements.py`): the two ported N-34a cases
now pass; new cases cover repeat reads and a restart (same conflict, cursor unchanged),
the correction (clean on repeat and after restart, two settlements in the ledger), a
revision that keeps an agreeing record (clean, documented limitation), and an
acknowledged market the ledger has not settled (stays skipped; positions and balance keep
flagging). Result **29 passed** (05:00:19Z). Balance mismatch still blocked in the
original reproduction; this change does not claim an unsafe order was demonstrated.

Residual limitations as of `500c993` (superseded below): an unmappable record stayed
acknowledged; a market the ledger had not settled stayed covered by its acknowledgement;
while one of several conflicting records still agreed, a further contradictory record was
only visible through positions and balance.

### 2026-10-23 follow-up: acknowledgement evidence scope (PR #39 review P2)

Codex's review of `bde8bc3` (local report `…/Vader-Intelligence-reviews/20261022-pr39/
review_probes/review.md`, read; its probe file reproduced here from a scratch copy
pointed at this worktree) showed the `any(record agrees)` rule accepting a whole group of
contradictory records: with the old YES record retained and a genuinely later NO/$0 or
scalar/$0.50 record added, reconciliation reported only `balance_mismatch` (balance
updated) or nothing (old balance), before and after a restart. **Reproduced at
`bde8bc3`: 4 failed / 1 passed (06:28:36Z), identical to Codex's result.**

Change (`f24af41`, `trading/executor.py`, `trading/gateway.py`, `trading/kalshi/gateway.py`):

- `settlement_fingerprint(record)` (scheme `vs1`) digests every field of a parsed venue
  record; `raw_settlement_fingerprint(item)` (`raw1`) digests an unparseable record as
  listed. `UnreadableSettlement.fingerprints` is additive (derived from `records` when a
  gateway gives none). The Kalshi gateway keeps every distinct record per token, in
  listing-independent order; parsed records beside an unparseable one for the same ticker
  are carried along instead of being read as the settlement.
- `settlement_evidence(gateway, ledger)` lists the records the executor reads next as
  `{token: {fingerprint: summary}}`; `acknowledge_settlement(..., evidence=...)` requires
  the fingerprints it resolves and records them under the key
  `settlement_ack:<token>:<digest(evidence)[:16]>` (data: actor, note, token, evidence).
  A token can carry several acknowledgements; an exact retry is a no-op; the same evidence
  under a different note is a `LedgerConflict`.
- Reconciliation skips exactly the covered records. Any other record under the token is
  reconciled as if nothing had been acknowledged: a readable one through `_settle` (so a
  disagreeing record of a ledger-settled market is `settlement_conflict` until an
  immutable `supersedes_id` correction), an unparseable one as `settlement_unreadable`;
  several uncovered readable records are still conflicting unless the ledger has settled
  the market and all agree.
- Legacy acknowledgements (`settlement_ack:<token>`, with or without data) name no
  evidence and cover nothing; the record is reported with its natural reason and a
  warning is logged. They never gain scope retroactively. No schema change;
  `processed_inputs` rows are additive. No real ledger holds a legacy acknowledgement
  (live trading never ran).
- Provider timestamps are not identity: a changed record under an unchanged
  `settled_time` has a new fingerprint.

Tests (`tests/trading/kalshi/test_kalshi_settlements.py`, **38 passed**, 06:35:45Z): the
suppression-blessing case is replaced by `test_a_new_contradiction_is_not_covered_by_an_older_acknowledgement`
(NO/$0 and scalar/$0.50 × balance updated / old balance; repeat reads, cursor kept,
restart, correction, clean after restart); exact repeated pair stays clean; duplicate and
reordered listings; changed content under the same source time; a revised foreign record
is reported again until acknowledged on its own evidence; a revised record of an
unsettled own market; legacy/data-less acknowledgement covers nothing (warning asserted),
scoped one clears, exact retry no-op, different note refused, restart; an unparseable
record beside a readable one; a gateway that names no evidence; the evidence summary
shape. Adapted copy of Codex's probe (evidence supplied at the acknowledgement call,
otherwise unchanged): **5 passed** (was 4 failed / 1 passed). Residual: `settlement_evidence`
re-reads the venue (the same read reconciliation makes); an operator who acknowledges a
record they did not inspect is outside the mechanism.

#### Internal review (subagent, labelled internal; not Codex's external review) and fix

An independent subagent reviewed `f24af41` in its own worktree with a fresh brief (no
verdict supplied) and twelve adversarial probes (scratch, not in Git). Its report
(07:06Z) found, by severity: **(1) high** — acknowledging one record of a conflicting
pair let `_acknowledged` hand the other record to `_settle`, which recorded it as the
ledger's settlement; **(2) medium** — covered records were no longer compared with the
ledger's settlement, so a mistyped operator settlement or an unsupported correction only
surfaced through the global balance check; **(3) medium** — uncovered records in a group
that agreed on result/value but differed in payout/fee were skipped while the same listing
without any acknowledgement was reported; **(4)** runbook/spec described the old call
(fixed in `b08d27c`, refined now); **(5–7) low** — the outer `settlement_evidence` mapping
was accepted as evidence, the group error label depended on listing order, mixed-type
evidence raised `TypeError`. Sound: fingerprint stability (`+00:00` vs `Z`, `0.00` vs
`0.0000`, `value` null vs 100), raw digests never raise, ten malformed legacy rows do not
crash and cover nothing, colon tokens cannot collide, the unacknowledged path is unchanged.

Fix `553fa99`: a group never records; a covered record is skipped but the ledger's
settlement must be supported on result and value by at least one record the venue lists
(covered or not), else `settlement_conflict`; an uncovered record in a group is
`settlement_conflict` when it disagrees, `settlement_unreadable` otherwise, until
acknowledged on its own evidence; a lone uncovered readable record still goes through
`_settle`; evidence must match `(vs1|raw1):<sha256>`; the group label is the minimum code.
Consequence for operators (runbook): after a correction, acknowledge the record that
supports it if it is not already covered. Intentional non-change: within an acknowledged
contradictory pair, the operator's choice between the two listed results is theirs
(either is supported); only the balance check shows which one the venue paid. New tests:
partial acknowledgement never records (YES-only, NO-only, repeat, restart, resolution);
typo settlement and unsupported correction conflict; new agreeing-but-different records
reported until acknowledged; evidence validation; label determinism. Reviewer's probes
re-run on `553fa99`: 10 of 12 pass; the two remaining "failures" are the probe that
asserted the outer mapping is *accepted* (now refused, i.e. fixed) and P2b (the
intentional non-change above).

Second internal pass on `553fa99` (same subagent, fresh instruction; 07:35Z): tests/trading
272 passed, ruff clean; eleven new adversarial probes (partial acknowledgements with
restart, typo settlement, venue dropping the agreeing record, unsupported correction,
void+readable own market, malformed evidence rows, all-unparseable acknowledged group,
acknowledged foreign ticker later settled by the operator, executor-recorded settlement
followed by a second agreeing record with a fee) all hold the safe property; P2b accepted
as the intentional non-change (the balance check distinguishes payouts; two acknowledged
listed records with the same payout but different labels are economically harmless).
**Verdict: no material issue remains.** Two low notes kept as limitations: two distinct
unparseable records under one token are summarised only as `{"error": code}` (the
operator tells them apart in the venue UI before acknowledging); the spec sentence it
flagged was rewritten in `c8f92fa`. This is an internal check; Codex's exact-SHA review
of the new head is still required.

## Task B — F50 analysis (no change to record meaning)

Reconstruction at the checkpoint, using the recovered commit's own test file run against
head source in a detached scratch worktree: `test_a_scored_game_stays_scored_after_a_refresh_whose_schedule_fetch_failed`
**fails at head** (paired n goes 1 → 0 after a refresh whose MLB schedule fetch returns
503) and the lineage test fails (no fallback exists); the other five pass. Against its
own source all **7 pass** (self-consistent).

Trace. `outcomes.join` selects the latest completed live-lineage `settlement-market`
attempt inside the view, then takes the mapping **of that attempt's own run** via
`settlement_targets.mapping_version_id` and its anchor fetch (`_target_mapping`). A run
whose schedule fetch failed records the Kalshi label but no mapping, so the join returns
`identity_unresolved(mapping_incomplete)` although an earlier complete run proved the
game. The governing spec (§5) fixes the *label* to the latest attempt and requires the
mapping to confirm identity, but does not say which run's mapping; the same-run rule is
the #33 repair against stale mappings (D-003). So current behaviour is a conservative
reading of #33, and F50 is its coverage cost: populations shrink with provider outages,
and a re-evaluation supersedes a scored item with an unscored one (both versions are
retained; nothing is rewritten).

Recovered `37c7bac` (not applied): when the selected run's mapping is incomplete and the
label is a finalized binary, it walks earlier live `ok` attempts, skips runs without a
mapping in view, and takes the first complete mapping only if it is `mapped`, its own
schedule observation shows the game `final`, and its own Kalshi record has the same
finalized label; any other complete mapping stops the search. Independent checks:
joins use the anchor fetch inside the view and `parser_version` ordering consistent with
the main join; the stand-in still passes the identity and D-003 start checks; a changed
label (A→B) fails closed until a complete refresh; synthetic runs are excluded for live
evaluations; the stand-in enters the outcome manifest. I found no mechanical defect.
The open point is interpretation: the same `evaluation-v2` label would then name two
outcome-selection rules for the same decision and view (D-001 principle), which Codex
also flagged.

Recommendation (owner decision, proposed D-017): keep the fail-closed default now. If
the fallback is wanted, adopt it as **`evaluation-v3`** (or an explicit versioned
`outcome_policy` in the manifest) with: validity = earlier mapping from a schedule
showing the game final and the same finalized label; invalidation = any newer complete
mapping, label change, or a stand-in start differing from the decision's; tests for
A→B→A label corrections, cancellation/reschedule before final, stale/missing schedule
over many refreshes, replay determinism and as-of boundaries before and after the outage.
It blocks nothing for a source merge; it affects shadow reporting coverage; it does not
touch live use. No new probability uses post-cutoff evidence either way.

## Decision table (reconciled against actual owner comments)

Actual owner comments found on GitHub: one, on #17 (2026-10-19 13:25 PDT): **D-001
accepted**. No other PR has an owner comment; the #8 fix list (2026-10-20) and the three
#37 reviews are reviewer posts. Nothing else is accepted.

| Decision | Current behaviour | Evidence | Recommended conservative choice | Exact change if chosen | Blocks |
|---|---|---|---|---|---|
| #8 plan r3 | Docs-only PR, reviewer "mergeable on its own", awaiting owner acceptance; its contract-2/schema-4 mechanism was superseded by D-001 (contract 3/schema 5) | #8 review 2026-10-19; fix list Part B.1 | Accept r3 with a revision note pointing to D-001 | One note in `docs/specs/f33-implementation-plan.md`; mark #8 ready | Source merge of the stack (bottom PR); not shadow or live |
| Q-M1-1 later-tick publication | A decision without a publication record is published by the next tick at its actual time: `allowed` inside the window, `missed` after; never backdated | `docs/forward-shadow.md` recovery; Oct 19 archives publish in the same tick (23:08:12Z) | Accept as is | None; record acceptance in `decisions.md` | Nothing; forward reports already count `missed` |
| D-002 rule v2 | Opt-in via collector `eligibility_rules`; default `rule-v1`; production collector still books 0 (all 8 candidates `unrecognized_full_game_rule`) | D-002 archive recheck; integration-readiness production observation | Accept for shadows now; production rollout as a separate reviewed change (Task C below) | None for source; production config change later | Production coverage only |
| D-003 schedule revisions | Sticky C; post-C moves unscored (`schedule_changed_after_decision`) | `outcomes.py:205-217`, tests | Accept | None | Nothing |
| D-004 contract 4 | Explicit no-DDL migration (schema 6); forecasters abstain explicitly | preflight below reached schema 6 and loaded three forecasters | Accept | None | Nothing (shadows with models need it; already used Oct 19) |
| D-005 NBA/NFL | Blocked on an authorized official source; no adapter | D-005 | Keep blocked; a framework does not supply a source | None | NBA/NFL only |
| D-015 `last_trade_gap_c` | Two meanings (history vs forward); unchanged; caveat on fs-market-v1 forward results | D-015; one forward value 0.005 | Keep caveat; define `fs-market-v2` before further model work | None now | Interpretation of model shadow results; not merge/shadow/live |
| D-016 selection default | `selection-v1` default; `selection-v2` opt-in (#36) | Oct 19 archives: v1 abstained, v2 derived 0.4150 | Keep default; every new cohort opts in (the recipe does) | None; optional later default change = new cohort hashes | Nothing; shadow operation uses opt-in |
| N-20c history tier | Live listing overrides historical by stage order; recovered `be6f71f` prefers historical without a `history-v1` bump (188–228 real tickers would switch) | commit message; review note | Keep current; adopt only as `history-v2` with re-derivation of affected datasets | New interpretation version + re-acquire | Nothing for merge/shadow/live; research reproducibility |
| F34 holdout slot | Forecast holdout evaluation (`training.evaluate_holdout`) and a holdout paper backtest (`backtest.run(segment="holdout", acknowledge_holdout=True)`) both go through `registry.evaluate_holdout` keyed by (model, dataset, pinned holdout key): **one slot, either kind**; first use consumes the dataset's holdout; no candidate is `validated` (D-009) | `research/backtest.py:325-345`, `registry.py:225-247` | Keep one slot either kind; the holdout stays untouched; decide only when a candidate is validated | None | Nothing now |

Routine, already-authorized work (regression tests, this repair, documentation) was not
put to the owner.

## Task C — original forward evidence, confirmed read-only

Both archives (`/Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-{rule-v2,models}/shadow.sqlite3`)
were opened with `mode=ro&immutable=1`; the driver logs and `shadow-evaluate.json` were
read. Nothing was modified. Confirmed, with no disagreement:

| Fact | rule-v2 shadow (schema 5, contract 3) | models shadow (schema 6, contract 4) |
|---|---|---|
| Decisions | 1 at C = 2026-10-19T23:08:00Z, created 23:08:12Z, `selection-v1` | same |
| Publication | verdict `allowed`; evaluation item `published_timely` | same |
| constant-v1 | 0.5, Brier 0.25 | same |
| midpoint-v1 | abstained `book_availability_after_cutoff` | same |
| models | — | 0.413415 / 0.438094 / 0.461393 (all rejected on validation) |
| Outcome | `binary_scorable`, y = 0, payout 0.0000 | same |
| Settlement refresh | run `d106c127…` status **partial**, `selection_truncated: true`, 20 targets (18 mapped, 2 quarantined) | run `0daa8549…` **partial**, same shape |
| Research run | `inconclusive`; U=E=K=FK=1, M=P=FM=FP=0, paired n=0, `scored_constant` 1 | same (evaluator `evaluation-v2`) |

`inconclusive` therefore means paired n = 0 (the midpoint abstained under
selection-v1), not U = 0. The selection-v2 midpoint 0.4150 is a derived read-only
recomputation (D-016), not a published forecast; the stored decisions are untouched and
remain under selection-v1. One game is not evidence of skill; the D-015 caveat stands.

### Recipe and preflight for the next bounded selection-v2 shadow

Validated with synthetic inputs in this session's scratch directory (not Git):
`db-init` → `settlement migrate` → `forecast migrate` → `--record-contract 3` →
`--record-contract 4` (schema 6) → `replay --fixture tests/fixtures/mlb.json` (synthetic,
no network) → archive-only `forecast tick --research-config … --model-registry …
--budget 15` (exit 3 `inconclusive`, no due opportunity, witness phases complete) →
`ops/manage.py configure … --test-label --shadow-research-config … --tick-budget 15
--shadow-model-registry … --forbid …` (exit 0, label `com.vader.test.collection.<hash>`,
`scheduler: not_installed`) → `plist` (rendered, registers nothing) → `status`
(`never_invoked`). `runner_from_registry` loaded all three forecasters (symbolic, gbm,
logistic; `fs-market-v1`) with no error. No `run`, `install`, scheduled job, archive or
provider call.

Recipe (new directory `D`, unique dataset; code at or above this branch):

```sh
D=/Users/ashwin/Documents/Vader-Intelligence-shadow/<date>-v2-models   # new, isolated
cp -R /Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-models/registry $D/registry
cp /Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-models/collector.toml $D/   # rule v1 + v2
cat > $D/research.toml <<'EOF'
[research]
dataset = "forward-shadow-v2-models-<date>"      # unique cohort
selection_version = "selection-v2"
forecasters = ["d316e849de67b8cd318606987d74208e866d8e1bba99d744179db788442b11b5",
               "a6d69da1f90519eedc146c1d6b8ea50fa4b464cea96132f159268cb5ee7bc407",
               "06891d035c2156ad5c52b9be5a78656e934eea1926016de22c6e93ffdb287a01"]
EOF
PY=<stable checkout>/.venv/bin/python; DB=$D/shadow.sqlite3
$PY -m vader_intelligence.cli --db $DB db-init
$PY -m vader_intelligence.cli --db $DB settlement migrate
$PY -m vader_intelligence.cli --db $DB forecast migrate
$PY -m vader_intelligence.cli --db $DB forecast migrate --record-contract 3
$PY -m vader_intelligence.cli --db $DB forecast migrate --record-contract 4
$PY <checkout>/ops/manage.py configure --checkout <checkout> --python $PY --database $DB \
  --collector-config $D/collector.toml --state $D/state --output $D/shadow-ops.json \
  --cadence 120 --deadline 90 --grace 5 --test-label \
  --shadow-research-config $D/research.toml --tick-budget 15 --shadow-model-registry $D/registry \
  --forbid /Users/ashwin/Documents/github-repos/Vader-Intelligence/data \
  --forbid /Users/ashwin/Documents/Vader-Intelligence-operations \
  --forbid /Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-rule-v2 \
  --forbid /Users/ashwin/Documents/Vader-Intelligence-shadow/20261019-models
$PY <checkout>/ops/manage.py --config $D/shadow-ops.json plist     # inspect only
# driver.sh copied from 20261019-models; change only D, W, the first slot (~72 min before C)
# and the shadow-evaluate time; finite slots, no launchd. Use --settlement-limit large
# enough for the archive's settled markets (20 truncated the Oct 19 refresh).
```

Preflight checklist before the first cycle: the game and C are named; `forbidden_paths`
list every real production and earlier shadow path; `research.toml` names a dataset no
earlier cohort used; `configure` exits 0 under the test label; `plist` shows the test
label; `status` is `never_invoked`; the registry loads three forecasters with no error;
disk free ≥ 1 GiB. Expected honest outcomes include `no_eligible_markets` outside games.

### Production rule-v2 rollout (future, reviewed; not done)

The deployed collector (`9a344209…`, schema 1) reads successfully but books nothing:
its rule parser excludes every candidate. Addressing it needs, in order: (1) source
merge of the reviewed candidate; (2) a backup of the production archive to a new path
with `ops/manage.py backup` and an `inspect` of the copy; (3) a bounded shadow under
rule v1+v2 at the merged head on an isolated archive (the Oct 19 shadows are evidence at
`622caad`, not at the merged head); (4) an operator window: `stop`, update the stable
checkout, set `eligibility_rules = ["rule-v1", "rule-v2"]` in the production collector
TOML (a new collector configuration hash, hence a new cohort), one manual `run`, confirm
eligible > 0 or an honest `inconclusive`, then `install`; (5) health watch for a day.
Collection on schema 1 needs no migration; forecasting on production is a separate
decision. Rollback: `stop`, restore the previous checkout and TOML, `install`.

## Verification (this session, macOS 26.6 arm64, Python 3.11.6, uv 0.12.19)

| Check | Result |
|---|---|
| `uv sync --locked` | 04:47:25Z, complete |
| Focused repair files at `1f62fcb` | 155 passed, 29 subtests, 48.57 s (04:49:40Z) |
| Repair regression groups at unrepaired `757c73f` (scratch worktree) | 15 failed / 8 passed, 9 subtests (04:53:03Z) |
| Ported N-34a draft at `1f62fcb` | 2 failed / 24 passed (04:53Z); after the fix 29 passed (05:00:19Z) |
| Recovered F50 tests vs head source / vs own source | 2 failed / 5 passed; 7 passed |
| `tests/test_release_validation.py` (probes) | 16 passed; the two F51 false-positive cases failed before the narrowing |
| `ruff check` / `ruff format --check` (src tests ops tools/worker_observer) | all checks passed; 252 files formatted |
| `uv build` sdist + wheel; fresh `uv venv`; installed-wheel smoke (resolved `/private/tmp/...` interpreter) | status `complete`, **54 checks**, schemas 1–6, legacy archives exercised (05:04:15–05:04:20Z) |
| Full combined native suite on the final code (`python -m pytest -q`) | **1516 passed, 2 failed, 29 subtests passed in 294.75 s** (05:04:15–05:09:10Z); the 2 failures are the known absent-`tmux_jev` observer baseline, identical to the unmodified candidate |
| Optional observer suite at pin `167f94359728a6356fb8a76e62f5cfdc16a6d881` | **not run**: the session's permission classifier refused installing `tmux-jev` from its Git pin, and no existing environment with that distribution exists under `~/Documents`. The core environment's two `tmux_jev` failures are the known baseline, not new. Command to run by hand: `python3 -m venv .venv-observer && .venv-observer/bin/python -m pip install 'pytest==9.1.1' 'ruff==0.16.9' 'httpx==0.28.1' 'tmux-jev @ git+https://github.com/uberspaceguru/tmux-jev.git@167f94359728a6356fb8a76e62f5cfdc16a6d881' && .venv-observer/bin/python -m pytest --noconftest -q tests/worker_observer` |

These are my executions; Codex's 1,494-pass run, Grok's Linux reviews and the earlier
Windows/Mac evidence are separate and were not re-run here.

### Verification of the 2026-10-23 follow-up (`f24af41`, same machine and environment)

| Check | Result |
|---|---|
| Codex probe (scratch copy) at `bde8bc3` | 4 failed / 1 passed, 1.37 s (06:28:36Z) |
| Settlement suite at `f24af41` | 38 passed, 4.60 s (06:35:45Z) |
| Adapted Codex probe at `f24af41` | 5 passed, 0.67 s |
| `tests/test_release_validation.py tests/test_integration_readiness.py tests/trading` | 290 passed, 35.69 s (06:36:26Z) |
| `ruff check` / `ruff format --check` (src tests) | clean |
| Full suite `python -m pytest -q` at `f24af41` | **1525 passed, 2 failed, 29 subtests, 290.61 s** (06:37:11–06:42:02Z); the 2 failures are the absent-`tmux_jev` baseline |
| Observer suite, existing pinned private env `…/Vader-Intelligence-observer-mac/.venv/bin/python` (direct-URL metadata verified: `167f94359728a6356fb8a76e62f5cfdc16a6d881`), `-m pytest --noconftest -q tests/worker_observer` from this checkout | **85 passed, 8.56 s** (06:37:20–06:37:29Z); no installation |
| Skills provenance (`docs/checkpoint-skill-provenance.json` vs `~/.codex/skills`) | 50 files match the pin |
| Installed-wheel smoke at `b08d27c` (`uv build --wheel`, fresh `uv venv`, `tests/integration/wheel_smoke.py`) | status `complete`, **54 checks** (07:09:24–07:09:27Z) |
| Settlement suite at `553fa99` | **43 passed**, 5.69 s (07:13:55Z) |
| Adapted Codex probe / reviewer P3 baseline probe at `553fa99` | 5 passed / 1 passed |
| `tests/test_release_validation.py tests/test_integration_readiness.py tests/trading` at `553fa99` | 295 passed, 37.64 s (07:14:48–07:15:26Z) |
| `ruff check` / `ruff format --check` (src tests ops tools/worker_observer) at `553fa99` | clean, 252 files |
| Full suite at `553fa99` | **1530 passed, 2 failed, 29 subtests**, pytest 299.81 s (wall 07:16:21–07:32:41Z under concurrent review runs); same two absent-`tmux_jev` baseline failures |

Codex's earlier numbers at `bde8bc3` (1,516 core, 94 focused, 85 observer, 54-check wheel)
are Codex's; the rows above are new executions on the new code. Installed-wheel smoke on
`f24af41` is reported in the final message of the assignment that produced this commit.

## Reverse path

Each change is a separate commit on this branch; revert the commit. No schema, record
contract, archive, service, credential or production state changed. The N-34a change
alters only when a reconciliation reports `settlement_conflict`; it writes nothing new
to a ledger. The F51 narrowing changes only which inputs the paste guard refuses.

## Recommended sequence

1. Codex reviews this draft against `fix/integration-readiness-20261022`; the N-34a and
   F51 commits are small and independently revertible.
2. Owner decisions from the table (none blocks a source merge except the #8 r3 note;
   F50 and N-20c are versioned-policy choices; F34 waits for a validated candidate).
3. Source merge of the consolidated candidate by Codex after its own review of the
   inherited stack; author branches stay recoverable.
4. Separately: the production rule-v2 rollout above, then a new bounded selection-v2
   shadow with the recipe; no live activation.
