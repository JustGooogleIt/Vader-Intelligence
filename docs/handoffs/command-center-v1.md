# Command Center v1 — development handoff

Assignment: `docs/handoffs/claude-command-center-build-20261022.md` (committed at
`f5e92b75da9567665fe84be28e35eb0b48a17f51`). Executor: Claude (this branch); Codex owns
integration, review and merges. Skills: Cool Coder 1.2.0 at
`e46e79805be0ee5877fa9bc993492064bbb40aa5` (`planner`, `system-design`,
`security-engineering`, `data-systems-design`, `implement`, `verify`, `code-review`);
50 manifest files re-hashed against the pin on 2026-10-23, none modified.

Everything below is a **development candidate**: nothing is deployed, scheduled,
merged or production-facing. All runs used a synthetic two-game archive and disposable
roots under the session scratch directory (outside Git).

## Checkpoint

| Item | Value |
|---|---|
| Branch / worktree | `feat/command-center-v1` at `/Users/ashwin/Documents/github-repos/Vader-Intelligence-command-center-v1`, private `.venv` (python3.11 + `uv==0.12.19`, `uv sync --locked`, no lockfile change) |
| Base | `origin/fix/integration-readiness-20261022` = `f5e92b75da9567665fe84be28e35eb0b48a17f51` (advanced from `72a86ae` by the assignment commit only) |
| Merged in | `fix/claude-release-validation-20261022` at `ad94063c37b56e9ecf8f61b31175b770bcf5e6fc` (ordinary merges `debce5c`, `6df82df`, `d182ec5`; no conflicts; both histories retained) |
| Main | `ebb58df9b89922db4828fda7d23891dd917a4b90` (unchanged; `git rev-parse origin/main` 2026-10-23) |
| PR #39 | head `ad94063c37b56e9ecf8f61b31175b770bcf5e6fc`: `f24af41` evidence-scoped acknowledgements, `553fa99` fix after the internal review, docs `b08d27c`, `c8f92fa`, `ad94063`; two internal review passes recorded in `docs/handoffs/claude-release-validation.md`; Codex's exact-SHA review still required |
| Candidate commits | `b23c961` spec/plan/executor/handoff; `30f97e7` J1/J2; `b93f082` J3; `d4eeacf` J4; `199b53b` command body bound; this handoff and any review-driven fixes follow (exact head in the final report) |
| Dependency map | `feat/command-center-v1` = integration branch (`f5e92b7`) + PR #39 branch (`ad94063`) + candidate commits. It depends on PR #39 and, through the integration branch, on #38's repairs. One draft PR against `fix/integration-readiness-20261022`; no further stack |

## What was built (implemented / simulated / blocked)

| Capability | Status | Where |
|---|---|---|
| Loopback dashboard (stdlib server, static page, CSP, read-only GETs) | **implemented** | `command_center/server.py`, `static/` |
| Overview, games with canonical identity and T−60 countdown, game workspace (pins, evidence timeline, frozen decisions + publication, settlement, evaluation, runs), farm view, engineering view; loading/empty/stale/unavailable/disconnected; source vs UI time; unknown ≠ zero; fixture labels | **implemented** | `projections.py`, `app.js` |
| Durable commands with receipts; duplicate clicks → one run; origin + session-token + JSON protection; no path/argv from requests | **implemented** | `store.py`, `server.py` |
| Game farms: `evidence_review_v1` with roles evidence_inspector, baseline_analyst, independent_checker as **fixed Python operations under the pods boundary** (not an LLM agent farm); `model_analyst`, `llm_agent` shown unavailable | **implemented** (honestly labelled) | `pods/game_ops.py`, `templates.py`, `farms.py` |
| Controller: coordinator-owned launches/cancellation, reconcile-before-dispatch on restart, global cap 4, per-run limits, one active game farm, pause/resume/cancel scope | **implemented** | `controller.py`, `farms.py` |
| Disposable tmux server per controller, one registered pane per live attempt, capture only through the reviewed observer | **implemented**; the real pinned observer verified a registered pane (identity matched), refused a replaced run option (no text) and failed closed after the server was killed | `tmux.py`, `panes.py`; evidence `observer-smoke.log` |
| Coding loop: frozen brief, writer worktree/branch, trusted checks on the exact commit, independent reviewer re-run + policy review, review packet, recommendation; nothing merges | **implemented**; one complete cycle demonstrated on a genuine issue with the **scripted (simulated) writer** | `coding.py`; evidence `review-packet.{json,md}`, branch `cc/5d84bb25/writer-1` |
| Real coding agents (`claude_cli`, `codex_cli`) | **blocked / disabled by design**: exact argv built from the inspected CLIs (Claude Code 2.1.309, codex-cli 0.153.4) and recorded; execution refused without `authorization.json` naming established $0-incremental usage; not exercised even with one in this pilot. A signed-in CLI is not proof of a billing allowance. **This is not a real-agent pilot pass.** | `coding.py` |
| Public-data probe (Kalshi/MLB GET readers) | **not run**: the dashboard pilot needs no live game; the fixture is labelled; the forward-shadow path is unchanged | — |

## Evidence (outside Git, session scratch)

`/private/tmp/claude-501/-Users-ashwin/d3d14058-dcfc-469e-836a-8b3bd9740261/scratchpad/cc-dev/`:

- `archive.sqlite3` (schema 6, synthetic two-game fixture `fixture-two-games.json`;
  reconstruction over 2026-09-27..28 `inconclusive`, no forward bindings), `command-center.json`,
  `root/` (orchestration store, Command Center store, artifacts, `coding/<run>/`, `tmux/`).
- `evidence/overview.png`, `game-824948.png`, `game-824949.png`, `farm.png`, `engineering.png`
  and `overview-dom.html`, `farm-dom.html`: rendered by **headless Google Chrome 155.0.8059.85**
  against the running dev server (the Claude-in-Chrome extension was not connected, so no
  interactive click session was recorded; the command path was exercised with the same
  headers a browser sends, including an origin-mismatch refusal, 403).
- Live game farm run `fbc5422f` (game 824948): start command `867ead15-…` accepted then
  applied; the same command id posted twice → `stored: false` (one run); stage 1 (two
  tasks) and stage 2 (checker) succeeded; outcome `consistent`; three registered panes
  (`%1`–`%3`) while live; read p95 ≤ 7.2 ms over 12–55 samples (`measure --n 50`: p50 1.7 ms,
  p95 2.3 ms, max 6.7 ms) against the one-second target.
- Live maintenance run `5d84bb25`: brief frozen (`brief.json`), writer commit
  `94f1c88808a61125ede6e6b03ac9414d132d56d3` on `cc/5d84bb25/writer-1` from base `d4eeacf`,
  writer and reviewer checks (ruff check, ruff format, pytest on the settlement and gateway
  suites) all exit 0 in separate worktrees at the same commit, verdict **accept**, candidate
  HEAD unchanged (nothing merged). The issue is genuine (internal review low finding 2 on
  PR #39: unreadable records indistinguishable in `settlement_evidence`); the patch was
  operator-written (`genuine-issue.diff`), so the **writer is simulated**; the pipeline,
  checks and review are real.
- `observer-smoke/observer-smoke.log`: pinned observer (`167f943…`) on a disposable server.

## Start / status / stop (development mode)

```sh
C=/Users/ashwin/Documents/github-repos/Vader-Intelligence-command-center-v1
D=/private/tmp/claude-501/-Users-ashwin/d3d14058-dcfc-469e-836a-8b3bd9740261/scratchpad/cc-dev
# server (loopback 127.0.0.1:8765) and controller (one per root; holds the coordinator lock)
$C/.venv/bin/python -m vader_intelligence.command_center serve --config $D/command-center.json
$C/.venv/bin/python -m vader_intelligence.command_center controller --config $D/command-center.json --deadline-seconds 14400
# status
curl -s http://127.0.0.1:8765/api/overview | python3 -m json.tool | head -40
$C/.venv/bin/python -m vader_intelligence.command_center measure --n 50
pgrep -fl "vader_intelligence.command_center (serve|controller)"
# stop (the controller kills its owned workers, records them lost, drops its tmux server)
pkill -f "vader_intelligence.command_center controller --config"
pkill -f "vader_intelligence.command_center serve --config"
```

URL: `http://127.0.0.1:8765/` (deep links `#game/824948`, `#farm/<run_id>`, `#engineering`).
The config's `forbidden_paths` list the production data directory, the operations
directory and the Oct 19 shadow directories; the server refuses to open an archive under
them. Pilot workers stop when their bounded runs end (no live attempts remain afterwards).

## Decisions

- D-CC-1: stdlib HTTP server + static page; no new dependency, no build step.
- D-CC-2: Command Center state in its own SQLite next to the orchestration store; writes
  serialized by SQLite (the server records commands, the controller applies them); the
  single controller per root is guaranteed by the pods coordinator lock.
- D-CC-3: real coding agents are not executed in this assignment (no authorization record
  of established $0-incremental usage); the adapter records the exact argv and refuses.
- D-CC-4: farm roles are fixed operations registered beside the research operations
  (`pods/game_ops.py`, role `diagnostics`), reusing the coordinator's leases, process
  groups, retries, quarantine and scrubbed environment; no new scheduler.
- D-CC-5: one active game farm in the pilot; global cap 4 processes (pods attempts +
  coding check processes); pod attempt budget = 2 × max_tasks (a retried lost attempt
  counts).

## Findings for Codex (not changed here)

- N-CC-1 (integration branch): `f5e92b7` added a top-level `history/` directory
  (`history/dashboard-and-game-farms-20261022.md`). Ruff now treats `history` as a
  first-party module, so `ruff check tests` reports four I001 import-order errors in
  `tests/datasets/test_backtest.py`, `test_collection_dataset.py`, `test_history_dataset.py`
  and `tests/integration/e2e_demo.py` on the integration branch itself (verified in a
  scratch worktree of `f5e92b7`: 4 errors with the directory, clean without it). Not a
  candidate defect; fix by moving the file under `docs/` (or configuring ruff's
  first-party detection). Lint reported here therefore excludes those four pre-existing
  errors; `ruff check src` and the Command Center tests are clean.

## Limitations and remaining decisions

- Private worktrees and process groups are not an OS sandbox; a maintenance patch that
  edits `conftest.py`/pytest configuration could run code as the controller user during
  the trusted checks. Protected paths cover `pyproject.toml`, `uv.lock`, the evaluator,
  the registry, scoring and `docs/evidence/`; the review packet lists every changed file.
  Owner decision: whether maintenance checks must run in a container before any
  non-scripted writer is enabled.
- `settlement_evidence` excerpt patch (`cc/5d84bb25/writer-1`) is a candidate for Codex to
  cherry-pick into PR #39 or the integration branch; it is not merged here.
- Pane capture needs `observer_python` (an interpreter with the pinned `tmux-jev`); without
  it the page shows `unavailable` with the reason. Panes exist only while attempts are live.
- Decisions for the game workspace come from `forecast_decisions`; the synthetic archive has
  none, so that section shows its empty state (labelled). Forward evidence is never produced
  by the dashboard.
- Internal (subagent) review of the candidate requested at `d4eeacf`; findings and fixes
  are recorded below when they arrive. Codex's external exact-SHA review is required.

## Verification

Recorded at the last checkpoint (see the final report for exact commands and counts).
