# Vader Command Center v1 — specification

Status: implementation specification for roadmap items J1–J4
(`docs/roadmap/dashboard-and-game-farms.md`), written 2026-10-23 on
`feat/command-center-v1` before implementation. Development candidate only: nothing here
is deployed, scheduled or merged into main by its existence. Skills applied: Cool Coder
`planner` and `system-design` (architecture and phase gates), `security-engineering`
(browser/request protection, untrusted text, path and command boundaries),
`data-systems-design` (durable command/run state, idempotency, recovery),
`implement` and `verify` (per-phase acceptance tests), all at
`e46e79805be0ee5877fa9bc993492064bbb40aa5` (1.2.0), unchanged.

## 1. Requirements (from the owner's roadmap and the 2026-10-22 assignment)

R1. A local browser application for one operator, bound to loopback by default, with an
    Overview, a Game workspace, a Farm view and an Engineering view.
R2. Every number shows its **source time** separately from the **UI refresh time**;
    loading, empty, stale, unavailable and disconnected are distinct states; unknown
    usage/cost is shown as unknown; fixture or reconstructed content is labelled.
R3. Each game and run is pinned to canonical identity (MLB `game_id`, event ticker, market
    tickers, run/task/attempt ids, source mode, code/config/protocol versions, input
    references). Ambiguous identity disables launch with an explanation. Two game views
    never blend tasks, artifacts, evidence, cohorts or panes.
R4. Frozen forecasts, analyst notes, actual publication, reconstruction, settlement and
    scored results are distinct. The forecaster, cutoff/witness, operational-veto and
    settlement rules stay authoritative; a model- or worker-written probability is never an
    official forecast.
R5. Farm controls (start, pause new dispatch, cancel owned work) use durable command ids
    and receipts; duplicate requests produce one run; the coordinator owns launches and
    cancellation; browser handlers spawn nothing; task state lives outside tmux; controller
    restart reconciles existing attempts before dispatching new work.
R6. tmux: a unique disposable server/socket owned by the controller; only registered panes
    are observed; missing/replaced/mismatched panes fail explicitly; observer advice stays
    disabled; terminal text never implies completion; no sends to user panes.
R7. Real coding/research agents run only through an adapter with an explicit execution and
    permission boundary and only with established, authorized, $0-incremental usage;
    otherwise the adapter is complete and tested and real execution stays disabled.
R8. Coding-improvement loop: separate maintenance farm, one coordinator, ≤ 2 writers,
    1 independent reviewer, under the global cap; isolated worktree/branch per writer;
    trusted checks against the exact commit; reviewers never approve their own patch;
    failed attempts preserved; no automatic merge.
R9. Bounds: one active game farm initially; at most four active agent processes globally
    (coordinator counted when model-driven); 5 s status refresh; 100 activity entries per
    page; bounded pane/artifact reads; measure p95 local read latency (target 1 s).
R10. Boundaries: no production checkout/archive/service change, no migration on real
    archives, no credentials, no paid services, no holdout consumption, no model search,
    no public listener, no distributed queue, no new database server.

## 2. Architecture decisions

| Decision | Choice | Why |
|---|---|---|
| Backend | Python stdlib `http.server.ThreadingHTTPServer` on `127.0.0.1`, JSON API, in a new package `vader_intelligence.command_center` | No lockfile change, no new server framework; the project already ships a CLI and stdlib-only ops tooling; one process, one operator |
| Frontend | One static HTML page with vanilla JS/CSS served by the backend (no build step, no CDN) | No Node toolchain in the locked environment; small maintainable surface; CSP forbids remote resources |
| Read model | Bounded projections computed per request from (a) the ops state directory (`ops.common.read_state`, `ops.runner.health/shadow_health`), (b) the archive opened read-only (`mode=ro`, `busy_timeout` 250 ms, bounded LIMITs), (c) the orchestration store opened read-only (`pods.store.status`), (d) the Command Center store | Application interfaces; short read transactions; a busy archive yields `unavailable`, never a hang |
| Write model | One durable Command Center SQLite (`command-center.sqlite3` in the orchestration root): `runs`, `run_tasks`, `commands`, `workers`; append-only `command_events` | Commands are durable before anything happens; receipts are replayable; the browser never holds state the controller needs |
| Controller | `python -m vader_intelligence.command_center controller --root R` holds the pods `Coordinator` (exclusive flock), applies accepted commands, dispatches template stages, reaps, reconciles on start | The coordinator already owns leases, process groups, cancellation, retries, budgets and quarantine; we add run scoping, not a second scheduler |
| Farm roles | Approved declarative operations under the existing pods boundary (`register_operation`), role `diagnostics`-class pods per run | Current workers execute no generated code; roles that need an LLM or a model registry are shown `unavailable` |
| Coding farm | `vader_intelligence.command_center.coding`: per-writer worktree + branch, patch application, trusted checks on the exact commit, deterministic reviewer; executors `scripted` (deterministic, labelled simulated) and `claude_cli`/`codex_cli` (argv built from inspected CLI capabilities; **disabled** unless an authorization record names established usage) | Honest separation of simulated vs real agent execution; $0 incremental spend |
| tmux | Per-controller socket `R/tmux/cc-<controller id>.sock`, `-f /dev/null`; one pane per attempt tailing its log; pane options `@vader_worker/@vader_task/@vader_run/@vader_pane_token`; observer config written by the controller; capture only through `tools.worker_observer` for registered workers | Reuses the reviewed observer and its identity checks; never the operator's server |

### Out of scope for v1

Remote access, multi-user, authentication beyond loopback + origin + session token,
model-driven coordinators, real provider calls, production launchd jobs, any change to
collection, forecasting, evaluation or trading code paths.

## 3. Data

Command Center store (schema `cc1`), SQLite WAL, foreign keys, writer `flock`:

```
runs(run_id TEXT PK, kind TEXT CHECK(kind IN ('game_research','maintenance')),
     created_at, state TEXT, pins_json TEXT, template TEXT, limits_json TEXT,
     pod_id TEXT, archive_digest TEXT, code_hash TEXT, protocol_version TEXT,
     paused INTEGER DEFAULT 0, finished_at, outcome TEXT)
run_tasks(run_id, task_id, role TEXT, stage INTEGER, PRIMARY KEY(run_id, task_id))
commands(command_id TEXT PK, kind TEXT, run_id TEXT, payload_json, received_at,
         state TEXT CHECK(state IN ('accepted','applied','rejected')), reason TEXT,
         applied_at)
command_events(id PK, command_id, kind, at, detail_json)   -- append-only (trigger)
workers(run_id, worker_id TEXT, role, attempt_id, pane_id, pane_pid, pane_token,
        registered_at, PRIMARY KEY(run_id, worker_id))
```

`pins_json` for a game run: `{game_id, event_ticker, market_tickers[], source_mode,
code_hash, protocol/config hash, collector config hash, archive path digest, evidence
snapshot sha256, cutoff, scheduled_start}`. A run's evidence snapshot is a
coordinator-built input artifact (`cc_game_evidence`, JSON) staged read-only to workers;
it is the only archive data a worker sees.

Task state is the pods store's (`tasks`, `attempts`, `task_events`); the Command Center
store only binds tasks to runs. No existing table or record contract changes. The archive
and the research registry are opened read-only by the server; the controller writes only
the orchestration root.

## 4. Security decisions

- Bind `127.0.0.1` only; refuse `--host` values that are not loopback unless
  `--allow-non-loopback` is given, which v1 does not implement (flag absent).
- Mutation only via `POST /api/commands` with: `Origin` header equal to the server's own
  origin; `Content-Type: application/json`; header `X-Vader-Session` equal to the
  per-process random session token (issued at `GET /api/session`, which is same-origin
  only); JSON body with a client-generated `command_id` (UUID4). Any failure → 403, no
  side effect. `GET` handlers never write. No CORS headers; no preflight answered.
- Response headers: `Content-Security-Policy: default-src 'self'; connect-src 'self';
  img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control:
  no-store`.
- No request parameter is ever used as a filesystem path or argv. Pane and artifact reads
  are keyed by `(run_id, worker_id)` / `(run_id, artifact sha256)` and resolved through the
  stores; artifacts are read through the content-addressed store with a 64 KiB cap.
- Untrusted text (worker logs, pane captures, artifact previews, reviewer notes) is
  rendered with `textContent` only; the page never uses `innerHTML` with data.
- Credentials: none are read; `VI_*` live secret paths, evaluator config, holdout and
  production archive paths are never passed to workers (the coordinator's allowlisted
  environment is unchanged). The server refuses to open an archive under a configured
  `forbidden_paths` list.
- tmux: only the controller's own socket; pane identity re-verified by the observer
  before and after capture; mismatches return `identity: unknown` and no text.
- Same-user processes are not sandboxed from each other; this is documented, not claimed.

## 5. Phase specifications and acceptance tests

### J1 — read-only Overview (server + projections)

Deliver `command_center/projections.py` (`overview`, `games`, `game`, `farms`, `farm`,
`engineering`), `command_center/server.py`, `command_center/static/`. Overview shows
upcoming games (latest eligibility per ticker, grouped by `game_id`, with reason, cutoff,
T−60 countdown computed from source time), collection/quote freshness (latest fetch time
from the archive, ops history latest cycle, shadow health), eligible/excluded counts,
coverage (markets with a quote observation in the last collection), active farms and
attempts, recent failures, budgets (pods budgets/usage; unknown usage shown as unknown).

Acceptance: `tests/command_center/test_projections.py` — synthetic archive (fixture
replay) yields games with correct counts; an empty archive yields `empty`; a missing
archive yields `unavailable`; a locked archive (writer lock held by the test) yields
`unavailable` within 1 s; stale state when the latest cycle is older than cadence+grace;
every projection carries `source_time` and `generated_at`. `test_server.py` — GET routes
are read-only (store digest unchanged), unknown routes 404, headers present, response
sizes bounded; p95 of 50 `GET /api/overview` requests measured and recorded.

### J2 — Game workspace and farm monitoring (read-only)

`game(key)` returns canonical identity (game_id, teams, scheduled start, event ticker,
market tickers, mapping/eligibility reasons with their source fetch ids), the evidence
timeline (eligibility checks, quote observations per market — bounded to the latest 100,
discovery passes, witness receipts), frozen decisions with publication status and witness
time (from `forecast.tick.inspect`), official results / settlement versions, evaluation
items that reference the game, and the game's farm runs. Everything labelled by `mode`
(`synthetic`, `forward_shadow`, `historical_reconstruction`) and source.

Acceptance: two distinct games from the fixture; each view lists only its own runs,
tasks, artifacts and panes (cross-check by run pins); a game with two eligibility rows
mapping to different `game_id`s is `ambiguous` and `can_launch=false` with the reason;
a decision after the game's cutoff shows a post-cutoff warning and the frozen probability
unchanged; a `synthetic` mode decision is never labelled forward evidence.

### J3 — bounded game farms (controls)

`command_center/templates.py`: template `evidence_review_v1` with roles
`evidence_inspector` → operation `cc_evidence_summary`, `baseline_analyst` →
`cc_baseline_analysis`, `independent_checker` → `cc_independent_check` (stage 2, reads the
stage-1 artifacts by digest); roles `model_analyst` and `llm_agent` are declared
`unavailable` (no registry input / no authorized provider). Operations are approved
declarative Python functions registered through `register_operation` with typed params
and declared inputs; outputs carry `"pod diagnostic: not a forecast, not promotion
evidence"`. `command_center/farms.py`: run creation from a game's pins (refused when
ambiguous), commands `start`, `pause`, `resume`, `cancel` with receipts, global cap
(active attempts across all pods + registered agent processes ≤ 4) and per-run limits
(max concurrent, max tasks, wall seconds). `command_center/controller.py`: loop that (1)
reconciles on start: reaps through the coordinator, finalizes runs whose tasks are all
terminal, re-applies `accepted` commands idempotently; (2) applies new commands; (3)
dispatches the next stage of running, unpaused runs within caps; (4) registers panes on
its own tmux server for live attempts and writes the observer config; (5) sleeps
`poll_seconds`. `pause` stops stage dispatch only; `cancel` requests cancellation of the
run's non-terminal tasks (the coordinator kills owned process groups) and marks the run
`canceled` when they are terminal; attempt directories are never deleted.

Acceptance (`test_farms.py`, `test_controller.py`): the same `command_id` twice → one
command row, one run; two `start` commands with different ids for the same game while a
run is active → second rejected `run_active`; cancel drains only the run's attempts (a
second run's attempt survives); controller restart with a live attempt re-adopts it
(coordinator lease/reap) and dispatches no duplicate task (task ids are content ids);
stale lease → `lost` → retry within `max_attempts`; global cap refuses a fifth process;
pane registration mismatch (option changed) → observer `identity: unknown`, no text;
worker text containing HTML/ANSI is returned as data and the page renders it inert
(server test asserts JSON string, browser check asserts `textContent`); no command
changes the collection configuration or state directory (digest check).

### J4 — coding-improvement loop

`command_center/coding.py`: maintenance run with roles `coordinator` (the controller,
deterministic), `writer` (≤ 2), `reviewer` (1). Executors: `scripted` applies a patch
file from the task's staged input in a fresh worktree/branch `cc/<run>/<writer>` and
commits it (labelled **simulated writer**); `claude_cli` / `codex_cli` build the exact
argv from the inspected CLI (`claude -p --permission-mode plan|default
--allowedTools … --max-budget-usd 0 --output-format json`; `codex exec --sandbox
workspace-write -a never --json -C <worktree>`), but `execute()` refuses unless an
`authorization.json` in the root names `{"usage": "established", "incremental_cost_usd":
0, "by": …, "at": …}` (not created by this assignment). Trusted checks: `ruff check`,
`ruff format --check`, focused pytest on the exact commit in the writer worktree, run by
the controller, recorded with argv, exit codes and SHA. Reviewer: a deterministic policy
review (protected paths untouched, no deleted tests, no weakened promotion/scoring
constants, checks green on the same SHA) that never reviews a patch its own worker id
produced; emits a review packet artifact and an accept/reject recommendation. No merge.

Acceptance (`test_coding.py`): one full cycle on a genuine issue (named in the handoff)
with the scripted writer: task frozen before execution (acceptance criteria hash),
isolated branch, checks on the exact SHA, reviewer ≠ writer, packet produced, run
`completed` with recommendation; a patch that deletes a test or touches a protected path
is `rejected`; `claude_cli` without authorization → `execution_disabled` with the exact
argv it would have run; budget exhaustion stops dispatch; a failed attempt is preserved.

## 6. Verification plan

Focused tests per phase; on the final candidate the combined Mac suite, lint/format,
installed-wheel smoke; a real browser session against the dev server on a disposable
archive and orchestration root with a disposable tmux server, screenshots retained outside
Git; p95 read latency measured; exact commands and SHAs in
`docs/handoffs/command-center-v1.md`.
