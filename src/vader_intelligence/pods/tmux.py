"""Optional tmux observation surface for running attempts.

tmux is observation only: the orchestration database is the source of truth, and killing
the tmux server (or never starting one) loses nothing. The session lives on a dedicated
socket (`tmux -L vi-pods`) so it never touches the operator's own tmux server. Panes run
read-only commands: `tail -F` of attempt logs and a status loop over a read-only store.

`python -m vader_intelligence.pods.tmux status --root ROOT` is the status-pane command.
"""

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time

from .store import open_store, status

SOCKET = "vi-pods"
DEFAULT_SESSION = "vi-pods"
COMMAND_TIMEOUT = 5


def plan_session(root, attempts, session=DEFAULT_SESSION, *, python=sys.executable, tmux="tmux"):
    """tmux argv lists: one status pane plus one `tail -F` pane per active attempt."""
    status_cmd = shlex.join(
        [python, "-m", "vader_intelligence.pods.tmux", "status", "--root", str(root)]
    )
    base = [tmux, "-L", SOCKET]
    commands = [[*base, "new-session", "-d", "-s", session, "-n", "pods", status_cmd]]
    for attempt in attempts:
        tail = shlex.join(["tail", "-n", "50", "-F", attempt["log_path"]])
        commands.append([*base, "split-window", "-t", session, tail])
        commands.append([*base, "select-layout", "-t", session, "tiled"])
    return commands


def open_session(store, session=DEFAULT_SESSION, *, which=shutil.which, runner=subprocess.run):
    """Create the observation session if tmux exists; report unavailability otherwise."""
    tmux = which("tmux")
    if tmux is None:
        return {"available": False, "created": False, "reason": "tmux not found on PATH"}
    attempts = status(store)["active_attempts"]
    commands = plan_session(store.root, attempts, session, tmux=tmux)
    for argv in commands:
        try:
            done = runner(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": True, "created": False, "reason": type(exc).__name__}
        if done.returncode != 0:
            return {"available": True, "created": False, "reason": done.stderr.strip()[:200]}
    attach = f"tmux -L {SOCKET} attach -t {shlex.quote(session)}"
    return {
        "available": True,
        "created": True,
        "session": session,
        "attach": attach,
        "panes": 1 + len(attempts),
    }


def close_session(session=DEFAULT_SESSION, *, which=shutil.which, runner=subprocess.run):
    tmux = which("tmux")
    if tmux is None:
        return {"available": False, "closed": False}
    done = runner(
        [tmux, "-L", SOCKET, "kill-session", "-t", session],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
    )
    return {"available": True, "closed": done.returncode == 0}


def status_loop(root, *, interval=5.0, iterations=720, out=sys.stdout):
    """Print the status summary repeatedly from a read-only store (bounded iterations)."""
    for i in range(iterations):
        store = open_store(root, readonly=True)
        try:
            summary = status(store)
        finally:
            store.close()
        out.write("\x1b[2J\x1b[H" if out.isatty() else "")
        out.write(json.dumps(summary, indent=1, sort_keys=True) + "\n")
        out.flush()
        if i + 1 < iterations:
            time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m vader_intelligence.pods.tmux")
    sub = parser.add_subparsers(dest="command", required=True)
    loop = sub.add_parser("status", help="print the read-only status summary periodically")
    loop.add_argument("--root", required=True)
    loop.add_argument("--interval", type=float, default=5.0)
    loop.add_argument("--iterations", type=int, default=720)
    args = parser.parse_args(argv)
    status_loop(args.root, interval=args.interval, iterations=args.iterations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
