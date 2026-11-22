# Claude continuation prompt — stabilize the release candidate

Copy the prompt below into Claude. Do not run it against the production checkout.

---

You are continuing Vader Intelligence after a status and integration audit.
Work in `JustGooogleIt/Vader-Intelligence`. The repository is intentionally public.
Codex owns integration and merge sequencing; you own this bounded validation and
remaining-finding task. Finish the acceptance criteria rather than adding features.
The consolidated review surface is draft PR #38. Do not treat its existence as
approval of the inherited stack or deploy its contents.

## Start from the actual integration checkpoint

Fetch `fix/integration-readiness-20261022` and resolve its full head SHA. Record it
before editing. Verify it contains consolidated repair code
`3b0ed7c7de90daf95e801410eaeffeb5e9a6470f` and author candidate
`757c73f986b4481beb2fbddbb596e58450fe2299`. If it has advanced since the handoff,
inspect the delta before continuing. Use a new worktree, private environment and
branch `fix/claude-release-validation-20261022`; never switch or edit another
session's checkout. Preserve all unrelated and ignored data.

Read AGENTS.md, plan.md's Integration I2, executor.md's Integration I2, then:

- `docs/handoffs/integration-readiness-20261022.md`
- `docs/roadmap/review-findings.md`, `docs/roadmap/decisions.md`
- `docs/handoffs/roadmap-handoff.md`, `docs/roadmap/requirements-matrix.md`
- `docs/specs/forecast-evaluation-v1.md`, `docs/forward-shadow.md`
- `docs/evidence/live-forward-20261019.md` and its machine-readable extract
- The latest actual GitHub review at #37, not just the author's summary.

Use applicable installed Cool Coder skills 1.2.0 pinned at
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. Name the exact skills used. Preserve
local modifications; do not upgrade shared skills, Python or unrelated tooling.
Read-only local inventory also found uncommitted roadmap notes, an older F47
repair draft in `Vader-Intelligence-wt-grok-h`, and settlement tests in
`Vader-Intelligence-wt-grok-i`. Inspect and account for their intent without
altering those worktrees. Do not duplicate an equivalent fix or discard their
evidence; port a genuinely missing regression only into your own branch.
Committed local repairs are now preserved on `recovery/grok-h-20261022`
(`05722483b6a2d6939b3db5a8af579ea3c779b6d1`) and
`recovery/f50-20261022` (`37c7bac13b6c1787243db6adc11dfa87b23e87ee`).
N-20c is preserved on `recovery/history-tier-20261022` at
`be6f71f1ba2a4b46a28f5fe812cbb333d197b3d5` (historical tier wins when a
ticker is in both listings, but history-v1 was not bumped). Review that concrete
delta when resolving N-20c; it is not in the integration candidate.
Do not merge a recovery branch wholesale: its older base lacks later
documentation restacks. Review only its intended patch and retain provenance.

## Task A — independently verify the integration repairs

Codex consolidated five small repairs above #37, overlapping the recovered
local implementations. Its test results are author
verification, not independent approval. Inspect the actual repair diff and rerun
the original negative scenarios independently, using disposable databases/files:

1. F39: one-shot `RuntimeError` while committing discovery summary. Confirm failed
   bookkeeping, no books and retained raw evidence. State the limitation if disk
   pressure also prevents writing the failure record itself.
2. F47: digest-valid but non-forward or malformed model artifacts must be refused
   at configure before files are persisted. A genuinely loadable forward artifact
   configures. Configuration and lifecycle loading have different responsibilities.
3. F48: timestamp-ID legacy initial deposit (`kind=deposit`, `note=init`) plus an
   equal init retry must preserve one immutable cash event. Different amounts and
   ambiguous duplicate legacy initializations must error without adding cash.
4. F49: registry removed or not initialized after setup. The model abstains with
   an explicit reason; constant/midpoint decisions continue if otherwise eligible.
   No replacement registry is initialized. Unknown/non-forward metadata still
   fails startup validation. Stop/uninstall remain possible.
5. F51: headerless and middle-body wrapped RSA PKCS8/PKCS1 fragments are refused.
   Complete parseable public-key bodies and normal hashes/config remain usable.
   Check bounded runtime and false positives. Do not claim universal secret
   detection; preserve the documented short/custom/JWK limitations. Synthetic keys
   only; never request/read a real credential or make a venue/provider call.

Add adversarial tests only for concrete risks. Do not rewrite the architecture or
silently expand these guarantees. Fix confirmed material issues on your own
branch in coherent commits and retain failing-before/passing-after evidence.

## Task B — resolve the remaining evidence semantics without rewriting history

First port and fix **N-34a** from the uncommitted settlement tests in `wt-grok-i`.
Codex applied those tests temporarily against the integration code: both YES→NO
cases fail (one/both venue records listed); the foreign/unreadable-record control
passes. `executor.py` skips acknowledged tokens before detecting later revisions,
so only `balance_mismatch` appears and the explicit settlement conflict is lost.
In this reproduction balance mismatch still blocks; do not overstate an unsafe
order as demonstrated. Scope acknowledgement to the intended resolved evidence,
continue checking venue revisions of this ledger's settled markets, preserve
foreign-record handling, and require an explicit conflict until an immutable
operator correction reconciles it. Synthetic doubles only. Add meaningful tests
for repeat/restart and correction behavior and document residual limitations.

Investigate F50 from the independent #33/#37 review: a newer settlement refresh
without an official schedule can turn an already scorable game into
`identity_unresolved`. Reconstruct it; trace settlement observations, mappings,
provider correction and evaluation as-of joins. Compare the actual governing spec.
Inspect recovered commit `37c7bac13b6c1787243db6adc11dfa87b23e87ee`: it allows
an earlier mapping only when supported by final MLB evidence and the same final
binary Kalshi label, but keeps evaluator-v2. Check its joins, precedence, parser
ordering, as-of ceilings and interpretation compatibility independently. It is
not part of the integration candidate and is not already approved.

Current conservative default: a missing prerequisite fails closed. Do not add an
unversioned "last good mapping" fallback. If current behavior is intentional,
document the exact claim, its coverage cost and regression tests. If a fallback
is warranted, propose a versioned policy with validity/invalidation criteria,
corrections A→B→A, cancellation/reschedule, stale/missing schedule, replay and
as-of behavior; record it as an owner decision before changing record meaning.
No new probability may use post-cutoff evidence. Never reuse unrelated evidence
to make a display or score look complete.

Reconcile the open decision list against actual owner comments. D-001 was accepted
October 19; do not reopen it or assume other decisions were accepted. Produce a
short decision table for #8 r3, Q-M1-1, D-002/003/004/005/015/016, N-20c and F34:
current behavior, evidence, recommended conservative choice, exact change needed
if chosen, and whether it blocks source merge, shadow operation or live use.
Avoid asking the owner to approve already-authorized routine implementation work.
Keep defaults unchanged where evidence meaning or holdout policy remains unsettled.

In particular, keep the holdout protected; clarify whether forecast evaluation
and a paper backtest consume the same one-time slot. Do not resolve that question
by running either on holdout. NFL/NBA expansion needs an authorized official
source; adding a framework does not supply it.

## Task C — reconcile real forward evidence and prepare the next bounded run

Read the original October 19 shadow reports and source archives read-only, using
paths in the existing local handoffs. If unavailable, say so. Do not alter them.
They recorded one eligible and timely published decision in each shadow,
constant scored, midpoint abstained under selection-v1; settlement was `partial`
because a 20-target limit truncated the refresh. The research report was
`inconclusive` because paired midpoint n=0, not because U=0. Confirm those facts
against actual retained evidence, and report any disagreement precisely.

Selection-v2's retrospective 0.415 midpoint is a derived/reconstructed result,
never a successful forward publication. Do not backdate, replace, or relabel
the original decisions. Do not infer model skill from one game or ignore that
all three models were rejected on validation. Preserve D-015's historical/live
feature interpretation caveat.

Prepare a concrete recipe and preflight report for a new isolated, finite
selection-v2 forward shadow with a unique dataset/cohort. Validate it with
synthetic inputs in your own scratch environment. Do not start scheduled jobs,
write operational archives, launch autonomous workers or make provider calls.
Source integration and production rollout are separate changes. The production
collector currently makes successful public reads but books zero because its
old rule parser excludes all eight discovered candidates. Document the future
reviewed rollout steps needed to address this without changing production now.

## Verification and delivery

Use `uv sync --locked`. Run the affected tests, combined native suite, lint,
format, build and fresh installed-wheel smoke on the exact final code. Keep the
optional observer dependency separate: the core environment's two missing
`tmux_jev` failures are not new regressions. Run the observer cases only in an
isolated environment at its documented upstream pin; no provider requests.
Resolve native path aliases for the installed-wheel harness. Never deselect a new
failure and call the suite green. Report actual commands, counts and timestamps,
separating your new execution from prior Codex/Linux/Windows evidence.

Maintain `docs/handoffs/claude-release-validation.md` with progress, findings,
decisions, checks, exact SHAs and next steps. Keep machine-specific paths,
archives, credentials, logs, captures, environments and generated output out of
Git. Inspect the staged diff and tracked files before committing.

Push coherent commits on your own branch and open a draft PR **against
`fix/integration-readiness-20261022`**, keeping the repair diff small. Do not
modify/retarget/close the original 30 PRs or merge anything; Codex will integrate
and handle the original stack after review. No force push, production update,
schema migration on real archives, service change, real order, canary, paid
service, holdout consumption, new model search or additional orchestration.

Return findings first, then exact head/PR, acceptance results, unresolved owner
decisions and the recommended merge/deployment sequence. If independent review
finds no material defects in the five repairs, say that explicitly and name the
exact cleared code SHA. Do not equate that scoped review with approval of the
entire 73k-line stack.
