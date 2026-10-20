# Research orchestration and pods specification v1

Status: implementation specification for roadmap items G1 (durable orchestration core)
and G2 (pods and candidate lifecycle), 2026-10-18.

## 1. Principles

1. The orchestration database is the source of truth. tmux is an optional observation
   and control surface; killing tmux loses nothing.
2. Pods run **approved, declarative operations** only (task types below with typed
   parameters). No agent-generated code is executed. Workers are local subprocesses for
   fault isolation and resource bounding; they are *not* a security sandbox and the
   documentation must never call them one.
3. Pods cannot reach live credentials, risk limits, the evaluator configuration, the
   final holdout, or archived results: none of these paths or handles are passed to a
   worker, workers run with a scrubbed environment and a private working directory, and
   only the coordinator writes protected state.
4. Every state change is a durable, idempotent transition; restart recovers from the
   database alone.
5. No paid model calls. Provider interfaces exist with deterministic test doubles; real
   vendor integrations are recorded as unexercised.

## 2. Package layout

`src/vader_intelligence/pods/`:

| Module | Responsibility |
|---|---|
| `contracts.py` | Typed task and result contracts (dataclasses + strict validation), task-type registry, deterministic ids |
| `store.py` | SQLite orchestration store: schema, append-only events, task/attempt/lease tables, budgets, idempotent result ingestion |
| `coordinator.py` | Scheduling loop (finite `step()` and bounded `run(max_steps, deadline)`), lease reaping, retries with backoff, cancellation, quarantine, budget enforcement |
| `worker.py` | `python -m vader_intelligence.pods.worker` entry: claims via lease token, heartbeats, runs the operation, writes a result manifest into its attempt directory |
| `operations.py` | Registry of approved operations (`train_model`, `symbolic_search`, `evaluate_candidate`, `robustness_analysis`, `paper_backtest`, plus `noop`/`sleep`/`fail` test operations). Each maps typed parameters to library calls |
| `providers.py` | `ModelProvider` protocol, `PaidCallsDisabled`, `ScriptedProvider` (deterministic double); no real vendor client |
| `planner.py` | Development-only planning context; `HeuristicPlanner`; `ProviderPlanner` (strict JSON, contract-validated proposals) (§9) |
| `autopilot.py` | Bounded planning rounds: drain, plan, validate, submit, record (`planner_round`), run; `accept_best` through the evaluator (§9) |
| `artifacts.py` | Content-addressed artifact store (sha256 path layout, write-once, verify on read) |
| `candidates.py` | Candidate lifecycle and promotion criteria (G2) |
| `tmux.py` | Optional: create/attach a tmux session with one pane per active attempt tailing its log; read-only status pane |

## 3. Store schema (SQLite, separate file `orchestration.sqlite3`)

WAL, FULL sync, foreign keys, writer `flock`. Tables:

- `pods(pod_id PK, role, created_at, config_json, config_hash)`; roles:
  `baseline_models`, `symbolic_search`, `robustness_execution`.
- `budgets(pod_id, kind, limit_value, window)`: kinds `max_concurrent`,
  `cpu_seconds_per_day`, `tasks_per_day`, `wall_seconds_per_task`, `memory_mb`,
  `api_cost_usd` (always 0 in this release).
- `tasks(task_id PK, pod_id, type, params_json, params_hash, experiment_manifest_hash,
  priority, max_attempts, created_at, state)` where `task_id = sha256(type, params,
  manifest)[:32]` so resubmitting identical work is idempotent.
- `task_events(id PK, task_id, kind, at, detail_json)` append-only: `submitted`,
  `leased`, `heartbeat`, `succeeded`, `failed`, `lost`, `retry_scheduled`,
  `cancel_requested`, `canceled`, `quarantined`, `ingested`.
- `attempts(attempt_id PK, task_id, n, lease_token, worker_pid, started_at,
  lease_expires_at, finished_at, outcome, workdir, log_path)`.
- `results(task_id, attempt_id, result_hash, result_json, ingested_at,
  PRIMARY KEY(task_id))`: at most one accepted result per task; re-ingesting the same
  hash is a no-op, a different hash for an already-ingested task is quarantined.
- `artifacts(sha256 PK, kind, bytes, created_at, source_task_id)`.
- Task states: `queued` → `leased` → `succeeded` | `failed` | `partial` | `canceled` |
  `quarantined`; `retry_wait` between attempts. Terminal states are absorbing.

Triggers forbid UPDATE/DELETE on `task_events`, `results`, `artifacts`. Mutable state
(`tasks.state`, `attempts.*`) changes only inside transactions that also append the
corresponding event.

## 4. Coordinator semantics

- `submit(task)` validates the contract and the pod's role permits the type.
- `step(now)`: (1) reap: attempts whose lease expired or whose worker PID is gone become
  `lost`; (2) ingest finished attempts' result manifests (validate schema, verify
  artifact hashes, copy artifacts into the store, record result) — malformed or
  hash-mismatched results → `quarantined` with reason; (3) cancel: kill process groups of
  `cancel_requested` attempts after a grace period; (4) schedule queued tasks by
  priority within per-pod `max_concurrent` and daily budgets; spawn workers with
  `start_new_session=True`, a scrubbed environment (allowlist: `PATH`, `HOME` set to the
  attempt dir, `LANG`, `PYTHONHASHSEED=0`, `TZ=UTC`, plus `VI_TASK_*` variables),
  `RLIMIT_CPU`, `RLIMIT_NOFILE`, `RLIMIT_FSIZE`, and a wall deadline enforced by the
  coordinator (process-group kill).
- Retries: `lost`/`failed` attempts are retried with exponential backoff up to
  `max_attempts`; deterministic failures (validation error, unsupported operation) are
  not retried.
- Heartbeats: worker updates `lease_expires_at` through a short transaction every
  `heartbeat_interval`; a worker that cannot renew stops itself.
- Restart: on start, the coordinator recovers from the store; no in-memory state is
  required. Duplicate worker for the same task is prevented by the lease token check at
  result ingestion (only the current attempt's token is accepted).
- Bounded: every loop has `max_steps` and a wall deadline; spawned processes are always
  reaped; the coordinator never leaves orphans on normal exit (`shutdown()` kills and
  records `lost`).

## 5. Experiment manifests and provenance

Every task references an immutable experiment manifest (stored as an artifact): dataset
manifest hash, feature set versions, split definition (development/validation only; the
final holdout is never included), code hash (`provenance.source_hash()`), operation and
parameters, seed, budget. Every result references the manifest hash, artifact hashes and
the attempt that produced it. Proposed strategies carry the full chain.

## 6. Candidate lifecycle (G2)

States: `proposed` → `validated` → `shadow_tested` → `paper_approved`; side exits
`rejected`, `quarantined`, `retired`. Transitions are performed only by the coordinator's
promotion service from evidence recorded by the protected evaluator:

- proposed → validated: development/validation metrics computed by the evaluator (not
  the pod) beat the declared baseline on paired opportunities with N ≥ `min_n` and the
  candidate passes stability checks; search budget and candidate history are attached.
- validated → shadow_tested: a forward-shadow (or explicitly labelled reconstruction)
  evaluation of the registered forecaster exists with coverage reported.
- shadow_tested → paper_approved: a paper-execution report under conservative execution
  assumptions exists, risk checks never bypassed, and an operator approval record names
  the candidate. Live eligibility does not exist in this lifecycle: it requires
  independent review and explicit user activation outside the pod system.
- Final holdout: evaluated at most once per candidate by the evaluator; the holdout
  ledger records consumption; a consumed holdout is never reported as untouched.

## 7. Tests required

Lease expiry and reaping; heartbeat renewal; crash of a worker mid-task (kill -9) then
retry; coordinator restart with running attempts; idempotent re-submission and
re-ingestion; conflicting result quarantine; budget enforcement (concurrency, daily CPU);
cancellation with process-group kill; scrubbed environment (no credential variables
visible to a worker; a worker cannot see the live-secret path); bounded loops and no
orphan processes after shutdown; malformed result manifests; provider double never makes
network calls.

## 8. Integration: real operations, inputs and the protected evaluator (2026-10-19)

The research operations are wired (`pods/research_ops.py`); nothing raises
`OperationNotWired` any more. The trust boundary is enforced as follows.

**Inputs.** An operation declares the artifacts it reads (`register_operation(...,
inputs=...)`). Submission refuses a task whose inputs are not stored with the declared
kind. Before spawning, the coordinator copies verified, read-only copies into the
attempt's `in/` directory with an `inputs.json` listing; the worker reads them only via
`OperationContext.read_input`, which re-verifies the digest and refuses links. No
registry, archive, ledger or holdout path is ever passed to a worker.

| Input kind | Built by | Content |
|---|---|---|
| `dataset_snapshot` (`vi-pod-dataset-v1`) | `evaluator.snapshot_dataset` | development + validation rows of one registry dataset under one split, plus the feature-set description; `snapshot.load` re-derives each row's segment and refuses any row outside it (a holdout row cannot be smuggled in) |
| `model_artifact` | an accepted pod result | the candidate's JSON model artifact (no pickle) |
| `execution_inputs` (`vi-execution-inputs-v1`) | `evaluator.execution_snapshot` | candle quotes around each validation cutoff, market metadata and archived settlements (`research/backtest.py`) |

**Operations.** `train_model` (logistic or gradient boosting via `training.fit` on the
development rows; validation diagnostics only), `symbolic_search` (`symbolic_search.fit`),
`evaluate_candidate` (diagnostics on development or validation; development is labelled
in-sample), `robustness_analysis` (validation slices: by month, regular/postseason,
tight/wide spread, single-feature dropout; paired delta vs market mid per slice) and
`paper_backtest` (validation forecasts executed against candle quotes; a small labelled
scenario grid over slippage, latency and assumed depth). Every output carries "pod
diagnostic: not promotion evidence". Search budgets are declared parameters
(`hyperparameters.search_seconds`, default 300; symbolic `max_seconds`), never the
remaining wall time: the budget is recorded in the artifact, so a retried task
reproduces the clean run's artifact digest (tested by killing a worker mid-task). A
search that stops on its time budget is still timing-dependent and is reported
`partial`.

**Protected evaluator** (`pods/evaluator.py`, coordinator process only):

- `accept_model` parses the result's model artifact, checks dataset, split and feature
  set against the snapshot the task named, registers it in the research registry (model
  id = content digest) and proposes a `model` candidate.
- `evaluate` recomputes the validation comparison from the registry's own rows and the
  registered artifact. Default criteria (`Criteria`): baseline `market:mid_c`, metric
  log loss, paired N ≥ 100, paired bootstrap 95% CI of (candidate − baseline) entirely
  below zero, and a negative paired estimate in each chronological half. The record
  attaches the producing task's search budget and the number of candidates proposed on
  the same snapshot (`candidate_history_hash`). Pass → `validated`; fail → `rejected`
  with the evaluator's reason. Each candidate is decided once.
- `record_shadow` builds `ShadowEvaluationRecord` from a stored forecast evaluation that
  scores the candidate's artifact as a registered forecaster (contract 4); mode is
  `forward_shadow` or `reconstruction` from the evaluation view.
- `paper_evidence` builds `PaperReportRecord` only from a coordinator-run
  `research.backtest.run` summary (maker model none, worst-of-bracketing book), never
  from pod output; `record_paper` also requires an `OperatorApprovalRecord`.
- `evaluate_holdout` requires a validated (or later) candidate, runs
  `training.evaluate_holdout` (registry holdout ledger: first use consumes, later uses
  are reuses) and records the candidate's one entry in the pod store's holdout ledger.

**CLI.** `vader pods init|register|snapshot|execution-snapshot|submit|run|status|accept|
evaluate|candidate|holdout` (holdout requires `--acknowledge-final`), and `vader backtest
run` (holdout segment requires `--acknowledge-holdout` and is ledger-recorded).

## 8b. Revisions after the independent review of #26/#27 (2026-10-19)

- **Pinned final holdout (D-014).** A dataset's final holdout start is recorded once in
  the registry (`holdout_pins`).
  - `snapshot_dataset` pins it at the first snapshot when it is absent, and refuses any
    split whose validation reaches it.
  - Snapshots are `vi-pod-dataset-v2`: they carry `holdout_from`, `load` refuses any row
    on or after it, and v1 snapshots must be rebuilt.
  - Holdout-ledger entries are keyed by the pin, so two splits with the same holdout
    rows share one entry.
  - Training, symbolic search and backtests apply the same guard.
- **Inputs are coordinator-only.** A worker result declaring a reserved artifact kind
  (`dataset_snapshot`, `execution_inputs`, `experiment_manifest`, `planner_round`) is
  quarantined (`reserved_artifact_kind`) and refused by the store. `accept_model` requires
  a snapshot stored by the coordinator whose pin matches the registry's.
- **Candidate-bound inputs.**
  - `evaluate_candidate`, `robustness_analysis` and `paper_backtest` must use the
    candidate's own snapshot.
  - Paper-backtest execution inputs may cover only the snapshot's validation markets.
- **Criteria.**
  - Baselines are whitelisted: `market:mid_c`, `market:pair_mid_c`, `constant`.
  - Criteria weaker than D-008 (N < 100, no CI rule, constant baseline) are refused
    unless `allow_weaker` (`--allow-weaker-criteria`). The decision and the evidence then
    carry `criteria_weaker_than_default`, and such a candidate can never take the holdout.
  - The evaluator records the model-producing tasks run on the snapshot next to the
    proposal count.
- **Decisions are made on the current state.** `promote(expected=...)` re-checks the
  state inside its transaction.
- **Paper evidence is bound to the candidate.** It needs a simulated, single-ledger
  `historical-paper-backtest` of the candidate's own model and dataset on the validation
  segment. A pod's own backtest never qualifies.
- **Holdout crash safety.** If the registry entry exists but the pod ledger lacks it, a
  retry records the same evaluation in the pod ledger (`recovered`) instead of failing
  forever. `HoldoutEvaluationRecord.holdout_use` says whether this was a consumption or a
  reuse.
- **Reproducibility.**
  - A symbolic search stopped by `max_seconds` is `partial` (wall-time dependent).
  - Accepted partial results are flagged in the candidate spec (`partial`).
  - Malformed grid values (non-numbers, booleans, non-finite) are refused by the
    contract.
  - A malformed input listing is a deterministic failure.
- **Not a sandbox (restated).** Workers receive `VI_TASK_DB`, the orchestration database
  path, and write their claim and heartbeat through it under the lease token. Approved
  operations never touch anything else there, but nothing at the OS level prevents a
  worker process from opening it. This is why only approved declarative operations run.

## 9. Autopilot: bounded planning of approved operations (2026-10-19)

The pods so far run what an operator submits. The autopilot decides what a pod runs next.
It stays within the same boundary: approved operations with typed parameters, and no
generated code.

**Planner.** A planner receives a development-only context and returns proposals of the
form `(operation, params)`.

- The context (`pods.planner.context`) holds:
  - the pod, its role and the operations planned for that role;
  - the snapshot's hash, feature set and version, feature names and development row
    count;
  - the pod's own tasks on that snapshot, with an allowlist of result fields;
  - the remaining budget.
- The allowlisted result fields are:
  - `train_model`: family, selected configuration, configurations tried, stop reason,
    grid, and `development_cv_log_loss` (the selected configuration's chronological
    cross-validation score on development rows);
  - `symbolic_search`: status, counters, expression, complexity, and
    `development_inner_log_loss` (its inner validation split lies inside development).
- The context never carries:
  - validation diagnostics: the pods still compute `diagnostic_validation_log_loss`, but
    it is not in the allowlist;
  - lease tokens, candidate state or anything about the holdout.
- **Why development-only:** planning cannot adapt to the validation rows, so validation
  stays the evaluator's.

**`HeuristicPlanner` (`heuristic-v1`).** Deterministic, with no model call.

- *baseline_models:* the first grid for each family (logistic, gradient boosting), then
  one refinement per round around the development-selected configuration. For
  logistic, an edge choice extends the C grid in that direction and an interior choice
  brackets it. For gradient boosting, the planner halves the learning rate, doubles the
  iterations and brackets the leaves. A family stops when its newest score does not beat
  its best earlier score by `min_improvement` (0.0005), or when a task fails.
- *symbolic_search:* the default search, then searches twice as large while the
  inner-split score improves.

**`ProviderPlanner` (`provider-v1`).** It asks a `ModelProvider` for one JSON object,
`{"proposals": [{"operation", "params", "rationale"}]}`.

- **Recorded:** request and response hashes, never the text. Each rejected proposal is
  recorded as its hash, its operation name (if a string) and the reason, truncated.
  Reasons can quote short contract messages.
- **Rejected responses:** text that is not JSON (including the non-finite constants
  `NaN` and `Infinity`), the wrong shape, more than 64 KB, or a non-zero reported cost.
  Any provider exception ends planning with stop `provider_error`.
- **Malformed values:** a malformed parameter value (wrong type, non-numeric or boolean
  grid value) is a recorded rejection, never an exception.
- **Rejected proposals:** a proposal that names an operation not planned for the role,
  carries extra keys, sets a snapshot field to another value, has grid keys outside the
  family's, or fails the operation's typed contract. Each rejection is recorded with its
  reason and never submitted.
- **Paid calls:** a real vendor raises `PaidCallsDisabled`, which ends planning (stop
  `paid_calls_disabled`). The provider planner runs only against the scripted double.

**Loop (`pods.autopilot.run`).** Each round:

1. **Drains** the pod's unfinished tasks on the snapshot. The coordinator runs every
   pod's queue. If the pod's tasks are still queued afterwards (a pod budget), the loop
   stops with `tasks_blocked`.
2. **Plans:** builds the context and asks the planner.
3. **Validates** each proposal and prepares its task: it stores the experiment manifest
   and computes the content task id, without submitting. The per-round and total limits
   are applied to *new* task ids before anything is submitted.
4. **Records** the round as a content-addressed `planner_round` artifact, chained by
   `previous` to the pod's last round on the snapshot (numbering continues across runs).
   The record holds the context hash, accepted task ids, rejections by hash and reason,
   provider hashes, new task ids and the planner's stop.
5. **Submits** the planned tasks only after their round is stored (the same path as
   `vader pods submit`), so no task exists that no round names.
6. **Runs** the coordinator until idle.

**Bounds and resume.**
- The loop ends when nothing new is proposed, or at the round bound (≤ 50 per run), the
  task bound (`max_tasks`, the pod's *total* tasks on the snapshot across runs, ≤ 200) or
  the wall-time bound.
- **Crash recovery:** a crash is recovered by running the loop again. Unfinished tasks
  are drained first. Planned but unsubmitted work is proposed again: identical for a
  deterministic planner, and its content id is the same, so nothing runs twice.

**`accept_best`.**
- **Which model:** the pod's best finished model on the snapshot by development score.
  Scores are compared only within the fold setting of its most recent scored task
  (`train_model` by `n_folds`; symbolic by kind), and the summary records how many tasks
  were compared.
- **Decision:** that model is accepted as one candidate and decided by the protected
  evaluator (§8). On a rerun, an already-decided candidate is reported, not
  re-evaluated; a rerun that finds a new best proposes and evaluates another candidate,
  and the evaluator counts it.
- **Known limitation:** a symbolic search selects on its inner split, so larger searches
  look slightly better there partly through selection. Validation by the evaluator stays
  the only judge.
- The holdout is never touched.

**CLI.** `vader pods autopilot --root R --pod P --snapshot SHA [--planner heuristic|vendor]
[--rounds N --max-tasks M --max-tasks-per-round K --deadline-seconds S]
[--accept-best --registry REG --min-n --metric --baseline]`. `--planner vendor` builds a
real-vendor provider, which is refused.
