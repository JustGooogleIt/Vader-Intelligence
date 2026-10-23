# Integration readiness — 2026-10-22

This is a new Codex checkpoint, not a replacement for earlier author or reviewer
evidence. The owner requested a status audit, integration ownership and a bounded
continuation assignment for Claude. No production change is part of this pass.

## Repository and activity

- Public repository: `JustGooogleIt/Vader-Intelligence`; visibility unchanged.
- Main: `ebb58df9b89922db4828fda7d23891dd917a4b90` (merged #6/#7).
- Latest author candidate: #37, `757c73f986b4481beb2fbddbb596e58450fe2299`.
- Integration branch: `fix/integration-readiness-20261022`, based on that candidate.
- Consolidated review: [draft PR #38](https://github.com/JustGooogleIt/Vader-Intelligence/pull/38).
- Consolidated repair code: `3b0ed7c7de90daf95e801410eaeffeb5e9a6470f`.
- At inspection, all **30 open draft PRs, #8–#37**, have heads that are ancestors
  of #37. They are one stack in one repository; no additional repository is needed
  to integrate those remote heads.
- The candidate differs from main in 275 files, +73,701 / −154 lines. This pass
  does **not** claim an independent review of that entire diff.
- Latest author push was October 20. No process named Claude was observed in the
  bounded process inventory. Neither fact establishes why the session stopped.
- Original author branches and PR settings are preserved. The consolidated draft
  is an additional review surface; do not close the original stack prematurely.
- A separate read-only local inventory found uncommitted roadmap notes, an
  equivalent F47 repair/test draft in the older `wt-grok-h` worktree, and a
  settlement test draft in `wt-grok-i`. They were left intact, not silently
  committed or discarded. Inventory and reconcile them before retiring worktrees.

Locally committed, unpublished repairs were also recovered. Most older worker
branches had already been cherry-picked into the stack, so ancestry alone was
insufficient; `git cherry` identified these remaining patches:

| Local commit | Purpose | Disposition |
|---|---|---|
| `4b94a119d6efeaa5ecc8c149ae4532640818a110` | F39 summary failure | Equivalent consolidated repair, original preserved |
| `d058fe749ec5d059762e172d802ef37d5f8a5166` | F48 legacy deposit | Equivalent consolidated repair with ambiguous-history refusal |
| `fb3834a7e583905c50289799ae3f1e3d95c410d8` | F49 unavailable registry | Equivalent consolidated repair; original has richer diagnostic text |
| `05722483b6a2d6939b3db5a8af579ea3c779b6d1` | N-33b malformed model JSON | Six regression cases ported; unhashable feature-set guard included |
| `37c7bac13b6c1787243db6adc11dfa87b23e87ee` | F50 final-mapping fallback | Preserved for review, **not applied**; changes evidence selection without an interpretation-version bump |
| `be6f71f1ba2a4b46a28f5fe812cbb333d197b3d5` | N-20c prefer historical tier when both listings contain a ticker | Preserved, **not applied**; source/endpoint semantics change while retaining history-v1 |

New remote recovery branches `recovery/grok-h-20261022` (first four) and
`recovery/f50-20261022` (F50) preserve those exact commits. Do not merge these
branches wholesale: they forked before later evidence/documentation restacks.
Review/cherry-pick only the intended delta if appropriate. No original branch
was modified and uncommitted edits remain in their original worktrees.
`recovery/history-tier-20261022` additionally preserves N-20c. A patch-equivalence
inventory of `grok-a` through `grok-i` found the other 32 worker fixes already
represented in #37, rather than 32 more missing changes.

Scope map: #8–#19 establish forecasting evidence and lifecycle semantics;
#20–#23 add historical research and models; #24–#27 add paper/live adapters and
research pods; #28–#30 add demos/performance/autopilot; #31–#37 repair reviews.
Code presence does not authorize deployment or establish predictive edge.

## Repairs in this checkpoint

The latest independent Linux review of #37 (review ID 5583135058, October 20)
still reported F39, F47–F51 and policy gates. Codex reconstructed local synthetic
regressions, then reconciled the recovered local implementations above. It did
not use the reviewer's unavailable reproduction files.

| Finding | Change | Verification boundary |
|---|---|---|
| F39 | Discovery-summary `RuntimeError` follows the existing failed-run bookkeeping path, with no books requested | One-shot summary write/space-guard error; a disk that also refuses failure bookkeeping can still prevent recording completion |
| F47 | Configure exercises the actual runtime forecaster constructor and verification before persisting configuration | Non-forward feature sets refused; good fixture is now a valid, loadable logistic artifact |
| F48 | Paper `ledger init` recognizes legacy timestamp IDs with `note=init`; equal retry adds no cash, conflicting/ambiguous initialization is refused | Existing cash event identities and amounts are unchanged |
| F49 | Missing/unopenable registry yields unavailable registered models rather than aborting the tick before baseline decisions | Native tests assert baseline 0.5 persists and model abstains; startup model metadata checks remain strict |
| F51 | Refuse headerless/middle PEM-shaped bodies with adjacent long base64 lines, while allowing complete parseable public-key bodies | Synthetic RSA PKCS8/PKCS1 fragments; conservative pattern detector, not proof that arbitrary short/JWK/custom-encoded secrets are absent |

No schema, record-contract, selection default, settlement fallback, holdout policy,
credential source, live activation or network endpoint change was made.

Before source changes, the new readiness cases produced **6 failed / 1 passed**;
the summary-write parametrization produced **1 failed / 1 passed**. These exposed
the five issue classes. The legacy deposit test first fails on duplicate cash;
after the repair it also checks conflicting amounts. Additional end-to-end tests
exercise baseline decisions when a registry disappears.

## Native verification

Environment: macOS 26.6 arm64, Python 3.11.6, private locked environment.
All synthetic databases, build output and captured test logs stay outside Git.

| Run | Result |
|---|---|
| Unmodified #37 combined suite | 1,478 passed, 2 failed, 29 subtests passed; both failures require the absent optional `tmux_jev` dependency |
| Repaired focused collector/ops/model/ledger/activation cases | 147 passed, 29 subtests passed (before two additional registry-to-tick cases) |
| First combined repaired run | 1,488 passed, 2 failed, 29 subtests passed in 282.62 seconds; the same two missing optional-dependency failures as the unmodified base |
| Final combined suite at code `3b0ed7c` | 1,494 passed, 2 failed, 29 subtests passed in 290.37 seconds; the same two missing optional-dependency failures |
| Ruff lint / formatting | Both pass, including the observer; 251 Python files formatted |
| Wheel build and fresh installed-wheel smoke | Complete, 54 checks; schemas 1–6, contract migrations 3/4, legacy schema-4 archive; package loaded from site-packages |
| Optional observer, separate existing pinned environment | 85 passed; pinned `tmux-jev` commit `167f94359728a6356fb8a76e62f5cfdc16a6d881`; no provider call |
| Final malformed-document / registered-forecaster suite | 20 passed after the N-33b guard and six recovered regression cases |
| Synthetic end-to-end and crash-recovery demos | Both complete; output retained outside Git |
| Recovered uncommitted N-34a settlement tests | 2 failed, 1 passed; see remaining gate below |

The wheel smoke requires a resolved `/private/tmp/...` interpreter path on this
Mac. Passing its `/tmp/...` symlink spelling fails the harness's import-location
prefix assertion; using the resolved path passes. No application fix was needed.

The 1,488-pass run preceded the final unhashable feature-set guard and six
recovered N-33b cases; the final exact-code rerun includes them. The focused
collector/ops/model/ledger/activation rerun with the two registry-to-tick cases
was 149 passed, 29 subtests passed in 23.86 seconds before the six N-33b cases.
These are Codex **author verification** results for the consolidated repairs. They are separate
from Grok's independent Linux review and earlier Windows/Mac evidence. No new
live integration check, endurance test, final-holdout evaluation or trade occurred.

Reproduce in a new checkout at this branch's exact head:

```sh
uv sync --locked
.venv/bin/python -m pytest -q tests/test_integration_readiness.py tests/test_discovery_boundary.py tests/forecast/test_registered_forecasters.py tests/ops/test_operations.py tests/trading/test_trading_cli.py tests/trading/kalshi/test_kalshi_activation.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests ops tools/worker_observer
.venv/bin/ruff format --check src tests ops tools/worker_observer
uv build --wheel --out-dir "$SCRATCH/dist"
uv venv --python .venv/bin/python "$SCRATCH/wheel-env"
uv pip install --python "$SCRATCH/wheel-env/bin/python" "$SCRATCH/dist/vader_intelligence-0.1.0-py3-none-any.whl"
.venv/bin/python tests/integration/wheel_smoke.py --python "$SCRATCH/wheel-env/bin/python"
```

`SCRATCH` must be a new, absolute, resolved directory outside all operational
archives/checkouts. The locked core environment intentionally lacks optional
`tmux-jev`; its two dependency tests fail there. For the separate observer suite,
follow the exact pin and private-environment procedure in `worker-observer.md`.

## Production observation — read only

Checked at 2026-10-23 04:26Z (October 22 PDT). The deployed version is still
`9a34420947f5722109186ff4897f0b1f6cb18f09`, archive schema 1. Several scheduled
passes completed during this investigation; the latest completed at
04:24:45.858848Z, runtime about 5.14 seconds, 10 retrievals and 16 observations.
Discovery was complete, errors empty, **books 0** and eligible games/contracts 0.
All eight candidates were excluded as `unrecognized_full_game_rule`.

This establishes a running collector with successful public reads, **not fresh
book collection** or eligibility under a newer rule. The old rule parser remains
an operational coverage limitation. An earlier approximately 50-minute gap was
also visible; cause is unknown. No claim of uninterrupted endurance is made.
Archive size was approximately 1.83 GB, WAL zero, free disk approximately 19.4 GB.
No restart, migration, config edit, new job or production write was performed.
Machine-specific paths, service settings and logs remain in the existing local
deployment handoff, not in this new Git artifact.

## Remaining gates and next owner

1. **Independent review of these five repairs** and integration of the original
   review's unresolved decisions. Passing tests alone do not clear 73k new lines.
2. **F50:** a newer settlement refresh without schedule evidence can make a
   previously scorable outcome unresolved. Current latest-attempt fail-closed
   semantics are preserved. Do not silently borrow an older schedule; decide and
   version any fallback with correction/invalidation and as-of tests first.
   The recovered local implementation named above is a concrete starting point,
   not a reviewed resolution.
3. **N-34a, reproduced:** `trading/executor.py` skips acknowledged settlement
   tokens before checking a later venue revision. The local draft's two cases
   (venue lists one or both records after YES→NO) get only `balance_mismatch`,
   not `settlement_conflict`; the unrelated/foreign acknowledgement control
   passes. Current accounting remains stale; balance mismatch blocks this
   specific reproduction, so it does not establish an accepted unsafe order.
   Tests were applied temporarily only in the integration checkout and then
   reversed. The original uncommitted draft remains intact in `wt-grok-i`.
4. **Policy:** #8 plan-r3 acceptance, Q-M1-1 late publication, D-002/003/004,
   D-005 source authorization, D-015 feature interpretation, D-016 selection
   default, N-20c interpretation version, F34 holdout-use policy. Existing
   conservative defaults remain; D-001 versioning already has owner acceptance.
5. **Evidence:** the October 19 shadow reports really contain one eligible,
   published, constant-scored decision in each archive, no paired midpoint score,
   and a partial bounded settlement refresh. `inconclusive` does not mean zero
   decisions. Selection-v2's derived midpoint is not a published forward forecast.
6. **Release:** inspect required checks, exact heads and current policy before any
   merge. Do not deploy automatically after a source merge. New collection-rule
   rollout needs its own review, archive backup/restore and bounded shadow evidence.
   At this inspection GitHub reports draft #38 mergeable with no status checks,
   and main unprotected. That is not correctness approval and does not clear the
   known defects or evidence-policy gates.

The research handoff reports no demonstrated edge over market midpoint; the final
holdout remains protected. Do not start another model search, install a trading
framework, add orchestration or activate the live adapter to address integration.

Codex owns integration and final merge sequencing. Claude's bounded assignment is
[`claude-next-20261022.md`](claude-next-20261022.md). A successful independent review
may permit a consolidated source merge; author branches remain recoverable until
their changes and review history are accounted for.

## Skills and reversal

Cool Coder engineering skills 1.2.0, pin
`e46e79805be0ee5877fa9bc993492064bbb40aa5`: `engineer-workflow`, `implement`,
`code-review`, `data-systems-design`, `systems-programming`,
`security-engineering`, `reverse-branching`. Shared skill files were not changed.
All 50 files covered by the baseline's five-skill provenance manifest still match
its recorded pinned hashes; no shared installation changes were made.

To abandon this checkpoint, close its draft and retain the branch. To undo its
five repairs after publication, make a new revert commit; never reset/force-push
the author stack. No persisted production data or external effects need reversal
because this pass made none. A future source revert would not reverse migrations
or operational writes; those remain separately gated.
