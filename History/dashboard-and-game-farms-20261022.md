# GUI dashboard and game-specific agent farms

Added at the owner's request on 2026-10-22. **Roadmap only; nothing here is
implemented or running.** This does not expand Claude's current stabilization
assignment or change production. Assumption: one operator, local browser UI on
this Mac; remote/multi-user access needs a separate design.

## Operator experience

- **Overview:** today's games, collector freshness, eligible/excluded contracts,
  book coverage, active farms, worker heartbeats, failed/partial runs and budgets.
- **Game workspace:** select a canonical MLB game; see its contracts, scheduled
  start, T−60 cutoff, identity/eligibility reasons and chronological evidence.
  A game can have multiple distinct farm runs without blending their histories.
- **Farm monitor:** assigned roles, task queue, attempts/retries, elapsed time,
  available usage/cost, artifacts and bounded output from registered tmux panes.
  Example research roles are evidence inspection, baseline/model analysis and
  independent checking; an unsupported role is shown as unavailable.
- **Results:** distinguish working notes, model proposals, frozen probabilities,
  actual publication, official results, settlement and scored evaluation. Show
  reconstructed, synthetic and forward evidence separately.
- **Engineering view:** proposed code improvements, branch/commit, check results,
  reviewer findings and promotion decisions. This depends on a future coding-farm
  adapter; current research operations do not execute agent-generated code.

## Delivery order and acceptance

| Phase | Deliverable | Acceptance gate |
|---|---|---|
| J1 | Read-only dashboard | Display genuine archive/task status with source time and UI refresh time; missing data, no eligible games, failure and disconnection remain distinct |
| J2 | Per-game monitoring | Two simultaneous game views show only their own identified tasks/artifacts/panes; ambiguous mapping fails closed; unregistered panes are never captured |
| J3 | Start/pause/cancel controls | One game-farm pilot completes under global and per-game budgets; repeated clicks create one command/run; cancel drains owned workers; restart reconciles existing attempts before dispatch |
| J4 | Improvement-loop visibility | A real coding-farm task produces an isolated patch and independently checked review packet; UI reports claims and verification separately |

J1/J2 follow stable reviewed read interfaces and can precede the full agent farm.
J3 reuses the existing coordinator rather than spawning workers from page handlers.
J4 follows the separate coding-farm pilot. Merging code and deploying it remain
separate actions; a green dashboard does not authorize either automatically.

## Identity, controls and evidence

Each run pins canonical game ID, event/market tickers, run/task/attempt identity,
source mode, code/configuration/protocol versions and input references. A game
title or pane number alone is insufficient identity. Shared collection evidence
may be referenced by multiple runs; results and cohorts remain explicitly scoped.

Controls choose an approved task template, worker/concurrency limits, time/task
budget and an explicitly authorized provider/usage budget before launch. Unknown
usage is shown as unknown; real provider execution remains unavailable until its
adapter and budget are configured. Pause stops new dispatch; cancellation targets
only that run's owned attempts and preserves evidence. Neither stops collection.
Commands require durable identities and accepted/running/completed/failed receipts.

The GUI reads bounded projections through application interfaces. It never writes
directly into collection/forecast journals or treats terminal text as task state.
Forecast cutoff, publication/veto and settlement rules stay authoritative. Later
agent notes cannot rewrite a frozen forecast or turn reconstruction into forward
success. Existing holdout and evaluation access restrictions remain in force.

## Provisional pilot bounds and design gates

Planning assumptions, not measured capacity: one operator, one game-farm pilot,
at most four active workers globally, five-second status refresh, 100 activity
entries per page, bounded terminal capture and a one-second p95 read target.
One visible page polling every five seconds is about 0.2 refreshes/second; task
execution, model latency and budgets need separate measurement before expansion.
UI refresh freshness must not change the existing forecast-input freshness rule.

Reuse evidence/registry/task stores and the optional observer. Task state is
durable outside tmux; browser closure must not abandon or duplicate work. Use
short read transactions; a busy archive yields an explicit unavailable state.
No distributed queue, cloud service or new database server is justified for this
pilot. Choose the UI framework and API shape during detailed planning.

Before implementation, specify read projections, game/run scope, command
idempotency, recovery/ownership and schema compatibility. Before enabling controls,
review local access, browser-origin/request protection, command authorization,
safe rendering of untrusted worker text, credential redaction and pane allowlists.
Local same-user processes are not a security sandbox; remote access remains off.

Required fault cases: stale/lost heartbeat, replaced/missing pane, wrong game/run,
unavailable source/archive, duplicate start/cancel, exhausted budget, controller
restart and a post-cutoff schedule warning. Preserve truthful status and frozen
evidence in each case. No credentials or terminal captures belong in Git.

## Existing implementation references

- `src/vader_intelligence/pods/store.py`: durable task/attempt records and leases.
- `src/vader_intelligence/pods/coordinator.py`: scheduling, ownership and cancellation.
- `src/vader_intelligence/pods/operations.py`: approved research task boundary.
- `tools/worker_observer/observer.py`, `docs/worker-observer.md`: registered,
  bounded observations; worker reports and advice are not verified completion.
- `docs/forward-shadow.md`, `docs/specs/forecast-evaluation-v1.md`: operational
  forecasting and evidence semantics that the dashboard must preserve.

Applied planning guidance: Cool Coder `planner` at
`e46e79805be0ee5877fa9bc993492064bbb40aa5` (1.2.0), with `system-design` consulted
for the incremental architecture outline. This records scope and gates; it is
not an implementation-ready executor specification.
