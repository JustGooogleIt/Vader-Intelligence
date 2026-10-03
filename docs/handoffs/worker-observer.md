# Worker observer prototype handoff

## PR #6 P2 follow-up (2026-10-03)

Started from verified draft PR head
`65d72a80a8ebd3b0d6ac2d44c559ecacecf98b59` in a separate `observer-fixes`
worktree, with a new private environment. Changes are restricted to this observer,
its focused tests and documentation. PR #7, forecasting and production are untouched.
Pinned Cool Coder `implement`, `code-review` and `systems-programming` applied at
`e46e79805be0ee5877fa9bc993492064bbb40aa5`; no shared skill changes.

1. SIGTERM/SIGINT now set a deferred flag instead of bypassing `finally`. The flag
   survives termination during Popen creation/handle registration. Main-thread-only
   handling restores prior handlers after bounded cleanup, then exits 143/130.
   Owned POSIX groups receive TERM, 250ms grace, KILL; the direct child is reaped
   with a 2s deadline and pipe reader joined with a 2s deadline. WNOWAIT reserves the
   leader PID until group signaling finishes; never signal pane/server metadata PIDs.
   Default SIGCHLD and exclusive child-wait ownership are required. SIGKILL of the
   observer cannot clean up; escaped groups and kernel-stalled creation/termination
   are not covered by user-space bounds. See the runbook's explicit limitations.
2. After capture, re-read metadata and verify canonical cwd against the same allowed
   checkout/workspace, then recheck identity. Mismatches leave terminal null and
   identity unknown, preventing preview/advice. Before/after checks are **not atomic**;
   change-and-revert races and malicious same-user processes remain outside the model.

Windows/Python 3.12.14 verification of this fix:

```powershell
.venv/Scripts/python.exe -m pytest --noconftest -q tests/worker_observer
.venv/Scripts/ruff.exe check tools/worker_observer tests/worker_observer
.venv/Scripts/ruff.exe format --check tools/worker_observer tests/worker_observer
git diff --check
```

**68 passed, 7 explicitly skipped native POSIX cases** (six signal/process cases
and one symlink case). Lint/format/diff checks passed. Four new directory-drift
regressions were also run against the original inspect method loaded from the
starting commit: all four failed as expected because it returned matched identity
with captured text. Fixed code passes all four. Directory/capture fixtures are
synthetic; no tmux/provider calls occur. Portable handler-restoration tests do not
claim native signal delivery verification.

Native tests use real kernel signals, child sessions and descendants, including a
stubborn child/grandchild, repeated SIGTERM, exited leader, an unrelated sentinel,
direct-child reaping and handler restoration. The creation regression instruments
Popen registration timing but launches real processes and delivers a real SIGTERM
before Popen returns its handle. These tests were **not run on Windows**.

Prime reported a successful real-tmux smoke on the **old** head
`65d72a80a8ebd3b0d6ac2d44c559ecacecf98b59`. That is separate reported evidence;
it does not verify either fix and is not a test performed by this session.

Required Mac verification, from a disposable checkout of the updated PR head:

```sh
# Create a new private environment; do not overwrite an existing one.
python3 -m venv .venv-observer-p2
.venv-observer-p2/bin/python -m pip install 'pytest==9.1.1' 'ruff==0.16.9' \
  'httpx==0.28.1' \
  'tmux-jev @ git+https://github.com/uberspaceguru/tmux-jev.git@167f94359728a6356fb8a76e62f5cfdc16a6d881'
.venv-observer-p2/bin/python -m pytest --noconftest -q tests/worker_observer/test_boundary_posix.py
.venv-observer-p2/bin/python -m pytest --noconftest -q tests/worker_observer
.venv-observer-p2/bin/ruff check tools/worker_observer tests/worker_observer
.venv-observer-p2/bin/ruff format --check tools/worker_observer tests/worker_observer
git diff --check
```

Then repeat the disposable real-tmux smoke in `docs/worker-observer.md`, setting
`OBSERVER_PY="$PWD/.venv-observer-p2/bin/python"`, against the updated head. Record
the exact SHA, Python/tmux versions, commands and exits. Keep PR #6 draft pending
that native verification and review; no merge or deployment is part of these fixes.

## Original prototype record (old head)

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
