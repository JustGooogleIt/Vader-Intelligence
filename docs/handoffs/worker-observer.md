# Worker observer prototype handoff

Base: merged integration `e4c2357daafb926fce9a52ed2d34aa5eed3f8523`.
Branch: `feat/worker-observer`, separate worktree and private environment.
Scope: `tools/worker_observer/`, `tests/worker_observer/`, this handoff and
`docs/worker-observer.md`. PR #5, production, collector, forecasting, settlement,
migrations, scheduler, configuration and main dependency lock are untouched.

## Decisions / contracts

- Inspected upstream `uberspaceguru/tmux-jev` at
  `167f94359728a6356fb8a76e62f5cfdc16a6d881`: README, SKILL.md, implementation,
  tests, MIT license and evaluation report. Installed that exact Git revision in
  the private test environment. Runtime bridge rejects a different direct-URL pin.
- Small upstream CLI wrapper for discovery/info/capture. Upstream lacks explicit
  socket selection and server identity, so trusted configuration sets TMUX context
  and read-only native format queries verify socket/PID/start time, pane PID, and
  operator registration markers before and after capture. Only registered panes
  are returned/captured; discovery necessarily sees bounded all-pane metadata locally.
- Bounds and fail-closed unknown reasons apply to subprocesses, output, metadata,
  status age/identity, and workspace artifact reads. POSIX no-follow traversal and
  process-group cleanup are implemented but not natively exercised here.
- Version-1 status is a worker report, not a receipt or verifier. Hash matches and
  claimed command exits/commit references remain untrusted. Delivery, acknowledgement,
  reported completion and independent verification are distinct; verification is
  always false. No action-dispatch path exists.
- Optional advice requires config opt-in, command opt-in and interactive approval
  of exact frozen selected metadata/text. Valid local status suppresses model use.
  Uses the pinned upstream typed `jev.ask`/`choice` source API rather than live
  `assess-pane` recapture. Custom rubric is not the benchmarked upstream prompt.
  Errors return local evidence; `done` never grants authority. No live calls made.
- Trusted boundary: operator-owned config, executables, workspace root, and same-user
  tmux server. Not a sandbox or authenticity proof against malicious local processes.
  Windows path tests assume no concurrent filesystem attacker. Arbitrary terminal
  output is not automatically secret-free; users must inspect the external preview.

## Verification actually run

Windows / Python 3.12.14, isolated upstream installation, pytest 9.1.1,
Ruff 0.16.9, httpx 0.28.1. No production platform shims or shared tool upgrades.

```powershell
.venv/Scripts/python.exe -m pytest --noconftest -q tests/worker_observer
.venv/Scripts/python.exe -m unittest discover -s ../tmux-jev/tests -p test_core.py
.venv/Scripts/python.exe -I tools/worker_observer/upstream.py panes --help
.venv/Scripts/python.exe -m tools.worker_observer --help
.venv/Scripts/ruff.exe check tools/worker_observer tests/worker_observer
.venv/Scripts/ruff.exe format --check tools/worker_observer tests/worker_observer
git diff --check
```

**61 focused tests passed**; **10 upstream offline core tests passed**. Ruff and
diff checks passed. `--noconftest` bypasses the existing Unix-storage test conftest;
this optional tool never imports storage. Real subprocesses exercised arguments,
timeout, overflow, failure, pin verification and CLI help. Pane/server discovery,
identity replacement, terminal output and provider results used explicit synthetic
fixtures/mocks. Echoed tests-passed text and anonymous counts never become verified
completion. Provider failures preserve local observations. No credentials requested,
printed or persisted; no actual provider request occurred.

Review: argument arrays only, no send/queue/daemon surface; unknown on mismatched
identity; no raw stderr/provider body leaks; bounded reads and no-follow POSIX paths;
status hashes never become acceptance proof. Applicable pinned Cool Coder skills:
`implement`, `code-review`, `security-engineering`, `systems-programming`, 1.2.0 at
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. Shared installation unchanged. The user's
narrow prototype scope supersedes historical executor/parallel-work ownership text.

## Next steps / limitations

Independent review of this draft and the disposable Mac smoke in
`docs/worker-observer.md`. Real tmux server selection, pane option behavior,
process-group cleanup and POSIX no-follow enforcement are **unverified here**.
Neither upstream evaluation numbers nor mocked model answers establish live accuracy,
cost or latency. Do not enable live advice without separate provider access and
explicit content-sharing consent. No deployment, production registration, forecasting
phase, worker retrofit or ongoing monitoring was started. Stop at this prototype.
