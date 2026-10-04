# Optional worker observer prototype

This is a local, one-shot inspection CLI, separate from the market collector.
It does not send input, schedule work, monitor continuously, close tasks, verify
acceptance, merge, trade, or change collection behavior. No existing agent is
assumed to emit its status format. All committed examples and tests are synthetic.

## Upstream inspected

Repository: https://github.com/uberspaceguru/tmux-jev
Exact pin: `167f94359728a6356fb8a76e62f5cfdc16a6d881` (package version 0.1.0).
Read README, SKILL.md, all four implementation modules, both test modules,
pyproject.toml, MIT license and docs/evaluation.md at that commit. No upstream
implementation is copied; the isolated installation retains its MIT license.

Actual interfaces: `panes` returns JSON metadata for all panes; `info --pane %N`
returns one metadata object; `capture --pane %N --tail 60` returns text. Exit 0
means that operation succeeded, not that a worker completed anything. Exit 1 is
a runtime failure and 2 a usage error. Missing server is an error, not empty success.
Upstream has no socket/host flags and no completion verifier. Its captured line
range includes screen lines as well as scrollback: `--tail` is not a byte limit.
It internally buffers command output; our outer byte cap limits retained/exported
output and the deadline bounds the child, not upstream's intermediate allocation.

The adapter supplies an explicit `TMUX=socket,pid,0` environment and an absolute
tmux executable directory on PATH. Additional read-only `tmux -S ... display-message`
checks enforce identity because upstream's metadata lacks server start/PID and
pane PID/registration markers. The [tmux manual](https://man.openbsd.org/tmux)
documents the format fields; this is not native verification on our Windows host.

Upstream `assess-pane` recaptures live text. To keep consent bound to a preview,
our optional bridge instead calls the inspected Python `tmux_jev.jev.ask` and
`choice` interfaces with the frozen selected snapshot and a small typed rubric.
This is an adapter-specific assessment, not the upstream benchmarked prompt.
The bridge checks the installed distribution's Git direct-URL commit and repository
before executing. This is a provenance guard, not attestation against local tampering.

Upstream's evaluation reports 55/56 judgments for its updated prompt on authored
synthetic cases, and separately discloses false `done` judgments on anonymous
success counts. Neither those scores nor its cost/latency claims validate this
adapter or demonstrate worker completion. No live model calls were made here.

## Isolated installation

On the intended Unix host, use an existing Python 3.11+ and tmux installation.
From this observer checkout, create a **new** private environment; do not overwrite
an existing environment or upgrade shared tools. No collector lockfile changes.

```sh
python3 -m venv .venv-observer
.venv-observer/bin/python -m pip install 'httpx==0.28.1' \
  'tmux-jev @ git+https://github.com/uberspaceguru/tmux-jev.git@167f94359728a6356fb8a76e62f5cfdc16a6d881'
.venv-observer/bin/python -I tools/worker_observer/upstream.py panes --help
.venv-observer/bin/python -m tools.worker_observer --help
```

Do not copy SKILL.md into a shared skill installation. Upstream's transitive
dependencies are not fully locked here; record `pip freeze` for a live smoke run.
The bridge is installed-source pinned, not an assertion of a reproducible toolchain.

## Registration and identity

Configuration is trusted operator input (not worker output), JSON version 1:

```json
{
  "version": 1,
  "upstream_python": "/absolute/observer/.venv-observer/bin/python",
  "tmux": "/absolute/path/to/tmux",
  "workspace": "/absolute/allowed-workspace",
  "host": "exact-socket.gethostname-value",
  "socket_path": "/absolute/tmux.sock",
  "server_pid": "123",
  "server_started": "1790760000",
  "allow_external": false,
  "workers": [{
    "worker_id": "worker-a", "role": "reviewer", "pane_id": "%7",
    "pane_pid": "124", "pane_token": "operator-generated-fresh-nonce",
    "checkout": "/absolute/allowed-workspace/checkout",
    "task_id": "review-1", "run_id": "unique-run-1"
  }]
}
```

Discover first, then register exact IDs, never titles. The operator explicitly sets
pane user options `@vader_worker`, `@vader_task`, `@vader_run`, `@vader_pane_token`
to the corresponding values. Change run ID and nonce for every new assignment;
refresh registration on server restart or pane respawn. The observer never writes
options. Same-user processes can forge them: they detect accidental replacement,
not a malicious worker sharing the tmux server/account. Checkout must be inside
workspace, and current pane cwd must equal checkout (subdirectory changes fail closed).

Each invocation checks the local hostname, discovers IDs, selects only the registered
pane, validates all identity fields, inspects metadata, optionally captures, and
rechecks the canonical allowed directory and all identity fields. Directory validation
also rejects a changed checkout/workspace resolution. A mismatch discards captured
text before returning it or offering external advice. These before/after reads are not
an atomic tmux transaction; a same-user adversary or undetected change-and-revert
is outside the trust model. Never use this output as authorization.

```sh
python -m tools.worker_observer --config /absolute/observer.json --worker worker-a discover
python -m tools.worker_observer --config /absolute/observer.json --worker worker-a inspect
python -m tools.worker_observer --config /absolute/observer.json --worker worker-a \
  --status relative/status.json inspect
```

JSON output binds worker/task/run, host/server, role, checkout, UTC observation time,
selected pane metadata, and bounded terminal evidence. `identity: unknown` plus a
reason distinguishes missing/mismatched observations from success; exit 1 means
unknown/configuration failure, 2 argument usage error. Exit 0 only means inspection
matched. Advisory/status failures remain explicit fields in a successful local
observation. No raw subprocess stderr or provider error bodies are returned.

Bounds: 16 registrations, 512 discovered records, 128 KiB discovery/info stdout,
16 KiB capture stdout (overflow fails closed), 60 requested scrollback lines,
5 seconds per local child, 20 seconds per optional assessment, plus bounded cleanup.
At most six local command invocations per inspect, with no retries/ongoing observation
loop. On POSIX, each invocation has one small supervisor (`owned_child.py`) and the
requested command. This is a subprocess implementation detail, not a worker manager.
The subprocess boundary must run on the main thread. SIGTERM/SIGINT handlers only
record a request, including during process creation; after Popen returns, cleanup
runs and exits with 128 + signal number (143/130), restoring previous handlers.
Repeated termination requests do not interrupt cleanup.

The supervisor is the direct child and leader of a fresh session/group. It starts
the command in that same group, reaps the command on completion, writes its exit
code through a private bounded pipe, closes its stdout and remains alive. The
parent requires both this completion message and captured-output EOF; closing
stdout alone is not completion. The private pipe is not inherited by the command.
The supervisor catches TERM/INT; those caught handlers reset on command exec, so
the command still receives ordinary termination signals. No `os.waitid`, kqueue,
Python upgrade, shell, or external process-monitoring dependency is required.

Cleanup signals only the Popen-owned group with SIGTERM, allows 250ms, then
escalates to SIGKILL, waits up to 2s to reap the supervisor, and joins the reader
for up to 2s. The supervisor stays live through the last group signal even after
the command exits; stubborn same-group descendants are still included. In
particular, cleanup does not signal a zombie-only group during ordinary completion
on Mac (which can return EPERM). A signaling error is reported, not swallowed, and
direct-child reaping is still attempted. Unexpected supervisor exit or a missing/
malformed completion message fails explicitly; it never becomes a successful command.

The parent never polls or reaps its group leader before its final group signal.
Default SIGCHLD and exclusive ownership of waits are required: callers must not
reap that child themselves, install an auto-reaping disposition, or use another
thread/library that waits for arbitrary children. With that precondition, even an
unexpectedly exited supervisor remains unreaped and reserves its PID/PGID until
cleanup ends. Known reaped handles are refused; no signal targets pane/server PIDs
or a previously reaped command PID. The final parent wait occurs after all group
signals, so PID reuse cannot redirect a later cleanup signal.

This is not a sandbox: descendants deliberately escaping the group are outside this
cleanup boundary, as are descendants changing credentials beyond our signaling
permissions. SIGKILL of the observer cannot run handlers/finally and may leave
detached children; an OS-stalled spawn or uninterruptible child cannot be guaranteed
to terminate within user-space deadlines. Direct-child wait timeout is explicit.
Windows exercises direct-child cleanup only, not native POSIX signal/group behavior.
Discovery temporarily reads all pane metadata because upstream lacks filtering;
only registered metadata is returned and only the selected pane is captured.

## Worker status version 1

See `tools/worker_observer/examples/synthetic-status.json` and its referenced
synthetic acceptance file. The timestamp intentionally expires; it is not live status.
Required exact keys: version, worker_id, task_id, run_id, updated_at, input_delivery,
acknowledged, reported_completion, commit, command, artifacts. Unknown keys fail closed.
Age must be 0..300 seconds; future timestamps and identity mismatches are invalid.
Commit is a 40-digit hex reference; command contains bounded argv and integer exit_code;
artifacts contains 1..16 workspace-relative path/SHA-256 pairs, each <=64 KiB.

- Input delivery: `unknown` or `reported_delivered`, never measured by this observer.
- Acknowledgement: an explicit worker boolean, returned as `reported` or `unknown`.
- Reported completion: the worker's boolean, distinct from actual success.
- Independently verified completion: **always false** in this prototype. Even a
  reported exit 0, plausible commit and matching files do not prove execution,
  acceptance coverage or task success. An external reviewer must verify those.

Paths/terminal text are data, never commands. No referenced argv is executed.
Reads reject traversal, absolute paths, links, nonregular/oversized files and hash
mismatches. POSIX traverses with descriptor-relative no-follow opens; Windows fixture
reads reject links but require a trusted, non-concurrently-mutating workspace.
The workspace root and config/executables must be operator-controlled; no sandbox
against another process with the same OS privileges is claimed. Artifact bytes are
hashed locally, never added to external assessment payloads.

## Optional external advice

Both config `allow_external: true` and invocation `--assess` are required. Valid local
status takes precedence and suppresses the call. Otherwise an interactive preview
shows exact selected metadata/text; type `SEND` to share that frozen snapshot.
Redirected stdin cannot consent. No automatic secret-redaction guarantee exists.
Inspect the preview yourself. Provider access must be provisioned separately;
no credentials are requested, stored or printed by the adapter. Local inspection
removes the provider key from child environments. Live advice uses upstream's key
handling, the default TypeSafe endpoint (base-URL overrides removed), and its model
setting; record the model separately for any authorized live evaluation.

Provider timeout/malformed response/error falls back to the unchanged local
observation with `provider_unavailable`. Advice always has `advisory_only: true`,
`completion_verified: false`, `authorization_granted: false`, even for `done`.
There is no action-dispatch path. The custom rubric/response parser is tested with
mocks only; quality, latency, cost and real provider access remain unverified.

## Disposable Mac smoke

Native implementation verification of the Mac compatibility follow-up is recorded
in `docs/handoffs/worker-observer.md`; earlier Windows evidence remains separate.
Use a disposable checkout/workspace and the isolated environment above. Nothing in
these commands targets the collector service or its tmux server.

```sh
export OBSERVER_PY="$PWD/.venv-observer/bin/python"
export SMOKE="$(mktemp -d /tmp/vader-observer.XXXXXX)"
export SOCKET="$SMOKE/tmux.sock"
tmux -S "$SOCKET" -f /dev/null new-session -d -s observer-smoke -c "$PWD"
TMUX="$SOCKET,0,0" "$OBSERVER_PY" -m tmux_jev.cli panes > "$SMOKE/panes.json"
# Inspect the discovered metadata before registration:
cat "$SMOKE/panes.json"
"$OBSERVER_PY" - <<'PY'
import json, os, pathlib, secrets, shutil, socket, subprocess
smoke = pathlib.Path(os.environ['SMOKE'])
panes = json.loads((smoke / 'panes.json').read_text())
assert len(panes) == 1  # Dedicated new server only.
pane = panes[0]['pane_id']
tmux = shutil.which('tmux')
def call(*args):
    return subprocess.check_output([tmux, '-S', os.environ['SOCKET'], *args], text=True).strip()
worker = dict(worker_id='smoke', role='synthetic', task_id='smoke-task', run_id=secrets.token_hex(16),
              pane_token=secrets.token_hex(16), pane_id=pane, checkout=str(pathlib.Path.cwd()))
for option, key in [('worker','worker_id'), ('task','task_id'), ('run','run_id'), ('pane_token','pane_token')]:
    call('set-option', '-p', '-t', pane, '@vader_'+option, worker[key])
worker['pane_pid'] = call('display-message', '-p', '-t', pane, '#{pane_pid}')
config = dict(version=1, upstream_python=os.environ['OBSERVER_PY'], tmux=tmux,
              workspace=str(pathlib.Path.cwd()), host=socket.gethostname(), socket_path=os.environ['SOCKET'],
              server_pid=call('display-message','-p','-t',pane,'#{pid}'),
              server_started=call('display-message','-p','-t',pane,'#{start_time}'),
              allow_external=False, workers=[worker])
(smoke / 'observer.json').write_text(json.dumps(config))
PY
"$OBSERVER_PY" -m tools.worker_observer --config "$SMOKE/observer.json" --worker smoke discover
"$OBSERVER_PY" -m tools.worker_observer --config "$SMOKE/observer.json" --worker smoke inspect
# Expect matched identity, no completion claim. No sends or provider calls.
tmux -S "$SOCKET" kill-server
"$OBSERVER_PY" -m tools.worker_observer --config "$SMOKE/observer.json" --worker smoke inspect
# Expect unknown + nonzero exit for missing server; preserve smoke evidence if needed.
```

Also change the disposable pane's run option before inspection and confirm mismatch
with no terminal evidence, then restore it manually. Record tmux/Python versions,
commands, exits and output. Native socket selection, process-group cleanup and
POSIX no-follow reads remain unverified until this smoke/targeted native tests run.
