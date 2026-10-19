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
