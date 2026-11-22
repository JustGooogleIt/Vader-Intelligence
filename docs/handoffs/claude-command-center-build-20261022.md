# Claude assignment: settlement closure and Vader Command Center v1

Copy the prompt below into Claude. This document prepares an assignment; it does
not launch Claude, workers, a dashboard or a scheduled job. It extends the previous
stabilization assignment only when the owner submits it for execution.

---

Build the next substantial Vader Intelligence milestone in
`JustGooogleIt/Vader-Intelligence`: close the remaining settlement acknowledgement
defect, then deliver a local GUI with game-specific research farms and a bounded
coding-improvement pilot. Codex owns final integration/review and merges.

This is sustained work suitable for a 12-hour-plus session, not a requirement to
consume time. Use a maximum 16-hour wall-time budget for this run, checkpoint
throughout, and finish earlier if the acceptance criteria are met. Do not idle to
hit a duration, wait for games to finish, or claim progress from elapsed time.
Context compaction is a continuation: resume from the durable checkpoint.

## 1. Verify the starting point and preserve other work

At assignment preparation:

- Main: `ebb58df9b89922db4828fda7d23891dd917a4b90`.
- Integration PR #38 / `fix/integration-readiness-20261022`:
  `72a86ae1bfe2526589310af3caa46a5f30a445c5` before this new prompt document.
- PR #39 / `fix/claude-release-validation-20261022`:
  `bde8bc302c3f4de418b3784140d703203d1b1d7c`.
- Earlier independently cleared consolidated repair code:
  `3b0ed7c7de90daf95e801410eaeffeb5e9a6470f`.

Fetch and resolve current full SHAs yourself. Inspect every advancement rather
than resetting to these historical heads. Verify containment of the consolidated
repair and the GUI roadmap. Work in your own checkout/worktrees, environments and
disposable archives. Never switch branches in another session's directory or
discard uncommitted changes. Keep the repository public.

Read AGENTS.md, plan.md, executor.md, and:

- `docs/handoffs/integration-readiness-20261022.md`
- `docs/handoffs/claude-release-validation.md`
- `docs/roadmap/dashboard-and-game-farms.md`, backlog.md, decisions.md and review-findings.md
- `docs/specs/forecast-evaluation-v1.md`, `docs/forward-shadow.md`
- `docs/specs/research-orchestration-v1.md`, `docs/worker-observer.md`
- The relevant source under `pods/`, `forecast/`, `evaluation/`, `trading/`,
  `ops/` and `tools/worker_observer/`.

Use applicable Cool Coder engineering skills 1.2.0 at
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. Preserve shared/local skill edits.
Record the exact skills applied. Write the requirements, architecture/data/security
decisions, phase specs and acceptance tests before implementing each phase. This
prompt authorizes implementation within the boundaries below; do not stop after
producing a plan or ask approval for routine reversible engineering choices.

## 2. Close the remaining P2 first, as a small repair

Codex reviewed PR #39 at its exact reported head. Its blocked observer check is
now satisfied: the existing private environment at
`/Users/ashwin/Documents/github-repos/Vader-Intelligence-observer-mac/.venv/bin/python`
has `tmux-jev` at `167f94359728a6356fb8a76e62f5cfdc16a6d881`; 85 observer tests
passed without installation. Verify metadata before reuse. Do not repeat failed
installation attempts or bypass a permission classifier.

The remaining P2 is `Executor._acknowledged`, `executor.py:560–562` at PR #39's
reviewed head. `any(record agrees with the ledger)` accepts a whole group of
contradictory records. The ticker-wide acknowledgement does not identify which
records the operator resolved.

Reproduce this exact scenario before editing:

1. A held market returns contradictory YES/$1 and NO/$0 settlement records.
2. Operator acknowledges those records and settles the ledger YES/$1.
3. Keep the old YES record listed and add a genuinely later scalar/$0.50 record
   with its own source identity, timestamp and corresponding revenue.
4. Reconcile, restart the executor and reconcile again. Also test a later NO record.
5. Test both an updated balance and the old balance still visible. Present behavior
   is only `balance_mismatch` in the first case and clean reconciliation in the
   second. Neither emits `settlement_conflict`; both survive restart.

Codex's independent probes produced **4 failed / 1 passed**, including an exact
repeat control. Local evidence, if present:

- `/Users/ashwin/Documents/Vader-Intelligence-reviews/20261022-pr39/review_probes/review.md`
- `/Users/ashwin/Documents/Vader-Intelligence-reviews/20261022-pr39/review_probes/test_pr39_ack_scope.py`

If unavailable, reconstruct from the scenario and existing synthetic exchange
helpers. Do not claim to have used unavailable files. The original N-34a cases
are fixed; the broader evidence-scope requirement is not. No real unsafe order was
demonstrated. Do not turn this into a live test.

Durably scope acknowledgement to the exact resolved evidence, with stable record
identities/content fingerprints or an equally defensible conservative mechanism.
An old agreeing record must not authorize a newly observed contradictory result or
payout. Provider timestamps alone are insufficient. Preserve immutable settlements,
explicit `supersedes_id` corrections, foreign-record handling and exact retries.
Legacy unscoped acknowledgements must not silently acquire stronger guarantees.
Prefer additive metadata/interfaces over a schema change; explain compatibility.

Replace the test that blesses suppression of a new contradiction. Cover binary
and fractional changes with older records retained, duplicate/reordered listings,
changed content with an unchanged source timestamp, repeat/restart, legacy/missing
scope, and correction resolution. Demonstrate failing-before/passing-after.
Keep the F51 false-positive repair and unrelated behavior intact.

Commit this repair separately, update the release-validation handoff and push it
to your PR #39 branch if you still safely own that worktree/branch. Otherwise use
a new repair branch and small draft PR against #39. Preserve all published history;
no force push or main merge. Obtain an internal independent review and explicitly
label it internal; Codex's external exact-SHA review is still required.

If the repair cannot satisfy its tests, keep farm mutation controls disabled and
continue only independent read-only GUI/design work. Never bury the P2 in a large UI diff.

## 3. Build an isolated development candidate

Use a separate `feat/command-center-v1` branch/worktree. Incorporate the latest
integration documentation and your repaired #39 through ordinary local merges;
resolve conflicts explicitly and retain both histories. This is a development
candidate, not a merge into main or approval of the inherited stack.

Maintain `docs/handoffs/command-center-v1.md` with progress, exact commits, checks,
decisions, next commands and blockers. Extend plan.md/executor.md for the phases.
Keep foundational repairs, backend interfaces, UI, research-farm controls and
coding-farm integration in coherent reviewable commits. Prefer one development
draft PR against the integration branch over another large dependency stack.

Inspect existing interfaces before inventing new infrastructure. Reuse
`pods/store.py`, `coordinator.py`, `contracts.py`, `artifacts.py`, approved operations
and the observer where appropriate. Current research workers execute declarative
approved operations, not arbitrary generated code: a coding-agent adapter is new
work and must have an explicit execution/permission boundary.

## 4. Deliver the local dashboard first — J1/J2

Create a usable local browser application, bound to loopback by default. Choose a
small maintainable frontend/backend after inspecting the existing toolchain;
document the choice. No cloud deployment, public listener, distributed queue or
new database server. Keep read projections bounded and use application interfaces.

The operator should see:

- **Overview:** upcoming games, collection/quote freshness, eligible/excluded
  contracts, coverage, active farms, failures and available resource/usage budgets.
- **Game workspace:** canonical identity, participants, contracts, schedule,
  T−60 countdown, mapping/eligibility reasons, evidence timeline, forecasts,
  publication status, official results, settlement and evaluation.
- **Farm view:** workers and roles, queued/running/retrying/completed/failed tasks,
  heartbeats, elapsed time, scoped tmux output and inspectable artifacts.
- **Engineering view:** issue/task, patch and exact commit, checks, reviewer findings,
  proposed promotion and actual integration state.

Use a coherent, readable desktop layout: game navigation, selected-game workspace
and worker/activity details. Make failures actionable. Include loading, empty,
stale, unavailable and disconnected states. Show source time separately from UI
refresh time; unknown usage/cost is unknown, not zero. Label fixture/reconstructed
content. Do not display decorative fake live metrics or market data.

Pin each game/run to canonical game ID, event/market IDs, run/task/attempt identity,
source mode, code/config/protocol versions and input references. Ambiguous identity
disables launch with an explanation. Shared evidence may be referenced; histories,
cohorts and results must not be blended. Demonstrate two distinct game views.

Keep frozen forecasts, working analyst notes, actual publication, reconstruction,
settlement and scored results distinct. Preserve 300-second input freshness,
cutoff/witness rules, operational veto timing and the existing authoritative
forecaster/evaluator paths. A model-written probability is not an official forecast.

Provisional pilot bounds: one operator; one active game farm initially; maximum
four active agent processes globally, including any model-driven coordinator;
five-second status refresh; 100 activity entries per page; bounded terminal/artifact
reads; one-second p95 local read target to measure rather than assert.

## 5. Wire bounded game-specific research farms — J3

From a selected game, let the operator choose an approved task template, roles,
time/task/concurrency limits and authorized execution mode. Provide start, pause
new dispatch and cancel-owned-work controls with explicit scope.

The initial template can use evidence inspection, baseline/model analysis and an
independent checking role. Ground each in actual available operations/adapters;
show an unsupported role as unavailable. Do not pretend fixed Python operations
or a heuristic planner are an LLM agent farm.

Use durable command IDs, validated inputs and command receipts. Duplicate clicks
must produce one run. The coordinator owns launches and cancellation; browser
handlers must not spawn arbitrary commands. Task state lives outside tmux. Closing
the browser must not duplicate or abandon a run; controller restart reconciles
attempts before dispatching new work. Pause preserves active work; cancel drains
only owned processes and preserves evidence. Neither changes collection services.

Establish a unique disposable tmux server/socket before discovery or capture.
Register host/server/pane/task/run/checkout identity. Observe only registered
panes; missing/replaced/mismatched panes fail explicitly. Keep observer advice
disabled by default, and never infer completion from terminal text or confidence.
No automatic prompt sending to existing user panes.

For real coding/research-agent launchers, inspect installed CLI capabilities and
permission/billing modes; do not guess flags or disable approvals. Use only
existing explicitly authorized account capacity. Incremental-spend ceiling is $0:
no API keys, new API billing, purchases or fallback paid provider. A signed-in CLI
alone is not proof of a billing allowance. If permitted usage or enforcement
cannot be established, finish the adapter and deterministic tests, leave real
agent execution disabled, and report the precise blocker. That is not a real-agent
pilot pass. Ask once for genuinely missing authorization while continuing independent work.

Private worktrees and process groups limit accidents; they are not an OS security
sandbox. Use the tool's existing workspace/permission controls. Do not pass secret,
production archive, evaluator or holdout paths to workers, or bypass system/tool
permissions to make the demonstration run.

## 6. Demonstrate a bounded coding-improvement loop — J4

Keep software-maintenance farms separate from game-research runs. Start with one
coordinator, at most two task workers and one independent reviewer under the
shared global cap. Each writer gets a distinct worktree/branch and owned files.
Persist assignments, attempts, budgets, immutable input/output references and outcomes.

Demonstrate: identify a genuine bounded maintenance issue → implement an isolated
patch → run trusted checks against its exact commit → independent review → produce
a draft PR/review packet → accept/reject recommendation → reassess remaining work.
Do not merge automatically. This first pilot needs one complete cycle, not an
unbounded recursive swarm or a manufactured claim of increasing intelligence.

Choose a real backlog issue or a concrete defect found during this work. A
deliberately injected demo bug is synthetic and must be labelled; it does not
establish a real maintenance win. Freeze the task's acceptance criteria before
worker execution. Reviewers do not approve their own patches. Record actual test
commands, exit codes and source commits; worker claims remain unverified until
the trusted check/review path establishes them.

Workers may propose test/benchmark changes in reviewable commits; they cannot
quietly weaken checks, scoring, promotion rules or protected data boundaries to
approve themselves. Preserve failed attempts and rejected candidates. Unknown
cost/runtime or missing artifacts must remain visible. Budget exhaustion stops
new dispatch; repeated failure triggers bounded retry/quarantine, not an endless loop.

## 7. Validate behavior, not just screenshots

Use disposable archives/orchestration roots and synthetic keys only. Verify:

- Two game views and multiple runs cannot mix tasks, artifacts, evidence or panes.
- Duplicate start/pause/cancel requests are idempotent; budgets are global and per run.
- Worker crash, stale lease, controller restart and UI reconnect preserve truthful
  state without duplicate work; cancellation drains owned groups and locks.
- Wrong identity/directory, pane replacement and malicious worker text cannot
  leak unrelated terminal content, execute markup/commands or authorize completion.
- Browser-origin/request protection and explicit command authorization cover
  mutation routes; GET remains read-only. No arbitrary command/path viewer.
- Missing/stale data and post-cutoff warnings preserve frozen probabilities and
  honest publication/coverage labels. Synthetic data never becomes forward evidence.
- Fresh initialization, supported archive compatibility, writer locks, replay and
  backup/restore still work for affected interfaces. No implicit archive upgrades.

Run focused tests after each phase. On final code run the appropriate combined
Mac suite, lint/format, backend/frontend builds and installed-wheel checks where
affected. Exercise the actual browser and disposable real tmux, retain screenshots
and evidence outside Git, and report exact commands and SHAs. A backend-only test
does not prove the browser UI; a stub runner does not prove real agent execution.
Reuse exact-commit completed evidence instead of repeating unchanged tests.

Existing baseline: Codex at PR #39 independently ran 1,516 passing core tests plus
two absent-optional-dependency failures, 94 focused tests, 85 observer tests in the
pinned private environment, lint/format and a fresh 54-check installed-wheel smoke.
The additional acknowledgement probes failed separately. Attribute new results to
your execution; never relabel those earlier results as tests of your new code.

An optional bounded public-data probe may use only the existing allowlisted Kalshi
and MLB GET readers in a new isolated development archive, within existing rate,
payload/retry/time limits. Use one eligible game if available. No eligible game is
inconclusive; demonstrate other behavior with clearly labelled fixtures and do not
wait overnight for a convenient result. No private venue endpoints or orders.

## 8. Non-negotiable scope boundaries

No production checkout/environment/config/archive/service changes, migrations on
real archives, main merges, original-PR rewrites, force pushes, live trading,
canaries, credentials, paid services, protected holdout consumption or new model
search. Do not install the four suggested external trading/model frameworks as
part of this assignment. Keep MLB scope and existing conservative defaults.

F50 and N-20c changes remain unapplied until explicit versioned-policy review;
do not introduce an unversioned fallback or rewrite earlier evidence. Add the #8
r3 supersession note pointing to D-001 if needed, but do not invent owner acceptance
or treat an old plan as a description of the current record contract.

This assignment permits owned disposable tmux servers and a loopback development
dashboard. It does not authorize a production launchd job or an unattended
provider loop outside the demonstrated limits. A healthy dashboard may remain
available in development mode for the owner to inspect; stop pilot workers when
their bounded run ends. Give exact start/status/stop commands and resource paths.

## 9. Work continuously and leave a resumable result

Make routine reversible decisions yourself. Use independent subagents where they
help, within the four-agent cap; assign disjoint files and give reviewers a fresh
task brief rather than the author's verdict. Do not modify another active worker's
checkout. External Codex review remains separate from internal subagent checks.

Checkpoint after each meaningful phase and before context exhaustion. Push coherent
commits/draft PRs; keep docs/handoffs/command-center-v1.md current. If blocked, record
the exact evidence/next command and continue independent work. Never repeat denied
actions blindly. Stop at acceptance, a genuine exhausted budget, or a blocker that
prevents all remaining authorized work; do not stop at the first passing unit test.

Finish with: PR #39 repair SHA/PR; development candidate SHA/PR and dependency
map; implemented versus simulated versus blocked capabilities; actual browser/tmux/
agent evidence; test/build results; development URL and start/status/stop commands;
remaining decisions/limitations; and a small exact-SHA review packet for Codex.

Success is a usable local command center and a demonstrated bounded improvement
cycle, with honest evidence. It is not proof of profitable forecasts, unlimited
autonomy, a cleared inherited stack or readiness for real-money execution.
