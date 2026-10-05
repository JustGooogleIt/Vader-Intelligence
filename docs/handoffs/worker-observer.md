# Worker observer prototype handoff

## Mac compatibility implementation (2026-10-04)

Implemented by Prime at the user's explicit request, starting from
`d9faeb8c4f31e8ddcf4616b8922b364b0aafe6c1` on `feat/worker-observer` in a new
standalone development checkout and private environment. Secondary owns PR #7;
that work, production and shared environments were untouched. This section is
**author implementation verification, not independent approval**. PR #6 stays draft.
The commit containing this section is the review target; use its exact Git SHA,
not the earlier review's head or Windows results, for the next review.

The user's scoped fix contract supersedes the historical collector phase/ownership
instructions. Applied pinned Cool Coder `implement`, `systems-programming`,
`security-engineering` and `code-review`, version 1.2.0 at
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. No shared skill or Python changes.

### Problem and final design

Native review of d9faeb8 found Python 3.11.6 on this Mac lacks `os.waitid`:
13 focused tests failed and real tmux discovery was refused before launch. A
non-reaping kqueue experiment exposed a second native detail: group signals may
return EPERM for a zombie-only group. That experiment is not the final design;
permission errors are not treated as successful cleanup.

- `boundary.run` launches `owned_child.py` as a fresh session/group leader. This
  small supervisor starts the requested command in its own group, waits for that
  command, writes a bounded completion message through a private pipe and remains
  alive. The command cannot inherit that status pipe. Command stdout remains the
  bounded capture stream; both actual completion and EOF are required for success.
- The supervisor catches TERM/INT without exiting; exec resets those caught
  handlers in the requested command. Cleanup sends TERM to the owned group,
  waits 250ms, sends KILL and then reaps its direct child with a 2s bound. Reader
  joining has its existing 2s bound. Descendants that ignore TERM are included in
  KILL, even when the requested command has already exited or closed stdout.
- The parent never polls/reaps the supervisor before its final group signal. Its
  live or unreaped PID reserves the group identity until cleanup is finished. A
  reaped command PID is never used for signaling. Known reaped leader handles are
  refused. No pane, tmux-server or worker-registration PID is a cleanup target.
- Default SIGCHLD and exclusive child-wait ownership remain required. Competing
  waiters and auto-reaping are unsupported; the code does not pretend to detect
  every external wait/reap race. No waitid/kqueue dependency or Python upgrade.
- Deferred SIGTERM/INT handling spans Popen creation and handle registration.
  Repeated signals leave cleanup running; previous handlers are restored and
  termination exits 143/130. Tests cover interruption before supervisor
  initialization as well as before Popen returns after command readiness.
- Signal errors remain explicit, with direct-child reaping attempted even after
  an error. Missing/malformed completion data or unexpected supervisor exit is a
  failure, not an invented exit-0 result. The ordinary startup and permission-
  failure paths are tested separately.
- `observer.py` and the directory-drift fix are unchanged. Post-capture mismatch
  still returns terminal null, identity unknown and unavailable advice.

This adds one stdlib-only helper process per local command, not another worker
orchestration layer, daemon, provider or dependency. The supervisor lives only
for that command's bounded invocation and cleanup.

### Native verification actually performed

macOS 26.6 arm64, **Python 3.11.6 without os.waitid**, tmux 3.7c, pytest 9.1.1,
Ruff 0.16.9, httpx 0.28.1. `tmux-jev` installed in the private environment at
`167f94359728a6356fb8a76e62f5cfdc16a6d881`; installation metadata verified.

- **85 focused tests passed**, no skips on this Mac.
- **16 native process tests passed**: ordinary launch, live supervisor identity
  through final group signal, real repeated TERM/INT, creation-time interruption,
  stubborn descendants, command exit before descendant cleanup, stdout EOF before
  exit, direct-child reaping, prior-handler restoration, unrelated live sentinel,
  rejection of already-reaped handles, explicit signal failures and missing results.
- **Both original independent reproductions passed** (SIGTERM orphan and persistent
  cwd drift). Only the checkout paths in the retained reproduction script were
  adapted to the isolated development checkout. The directory reproduction retains
  its original synthetic runner; the separate tmux drift case uses a real worker.
- Disposable real-tmux smoke passed **18 cases plus three CLI invocations** using
  two fresh explicit sockets and synthetic workers. Server ownership, distinct PIDs
  and expected pane inventory were established before discovery/capture. Inherited
  TMUX intentionally named the other disposable server. Correct capture contained
  only the selected synthetic marker. Host/server/pane/task/run/token mismatches,
  pane disappearance/replacement and stable wrong directory failed explicitly.
- A real worker changed cwd between capture and postcheck with PID unchanged;
  captured text was discarded and advisory remained unavailable even with opt-in.
  The harness prohibited assessment/send commands. All test workers, servers,
  sockets and temporary directories were confirmed removed/inactive.
- Ruff lint, format and `git diff --check` passed. No core collector suite/build
  was repeated: application package, storage and collector are unchanged.

These are Prime's Mac results on this implementation. Earlier Secondary Windows
results below are historical and do not validate this new supervisor on Windows
or Linux. No live provider request or model-quality test occurred.

### Setup and verification commands

From a fresh isolated checkout of the implementation commit:

```sh
python3 -m venv .venv
# pip or the existing trusted uv may install only into this private environment.
.venv/bin/python -m pip install 'pytest==9.1.1' 'ruff==0.16.9' 'httpx==0.28.1' \
  'tmux-jev @ git+https://github.com/uberspaceguru/tmux-jev.git@167f94359728a6356fb8a76e62f5cfdc16a6d881'
.venv/bin/python -m pytest --noconftest -q tests/worker_observer/test_boundary_posix.py
.venv/bin/python -m pytest --noconftest -q tests/worker_observer
.venv/bin/ruff check tools/worker_observer tests/worker_observer
.venv/bin/ruff format --check tools/worker_observer tests/worker_observer
git diff --check
```

Repeat the disposable smoke in `docs/worker-observer.md`; use only newly created
explicit sockets, synthetic workers and this checkout's environment. The local
implementation evidence report retains the original reproductions, extended
real-tmux harness, exact commands, timestamps, result JSON and file hashes. It is
linked in the implementation handoff response; machine paths/captures/logs are
kept out of Git.

### Review handoff and remaining limits

Next: independent review of this exact implementation commit, focusing on the
private completion pipe, no-reap-before-final-signal invariant, creation-time
termination, unexpected supervisor exit and retained directory validation. No
merge/deployment approval is implied by the author's passing tests.

SIGKILL of the observer cannot run cleanup and may orphan the supervisor/command.
Deliberately escaped groups, changed credentials beyond signaling permission,
competing child reapers and OS-stalled spawn/termination are outside the guarantee.
A grandchild may remain briefly as a zombie awaiting its OS reaper; the direct
supervisor is reaped by this parent. Directory before/after reads remain non-atomic
and do not defeat malicious same-user change-and-revert behavior. No provider
accuracy/cost/latency claim. Linux/Windows execution of this new code remains for
independent cross-platform verification. No further feature work is started.

## Historical PR #6 P2 follow-up (2026-10-03, superseded process design)

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
