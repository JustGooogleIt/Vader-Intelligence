"""A disposable tmux server owned by one controller: unique socket under the root,
`-f /dev/null`, one window per live attempt tailing its log, pane user options set for
the observer's identity check. Observation only: the orchestration store is the source
of truth and killing this server loses nothing."""

import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

COMMAND_TIMEOUT = 5
OPTIONS = ("@vader_worker", "@vader_task", "@vader_run", "@vader_pane_token")


class TmuxUnavailable(RuntimeError):
    pass


class TmuxServer:
    def __init__(self, root, *, tmux=None, runner=subprocess.run, session="vader-cc"):
        self.root = Path(root)
        self.tmux = tmux or shutil.which("tmux")
        self.runner = runner
        self.session = session
        self.directory = self.root / "tmux"
        name = f"cc-{secrets.token_hex(6)}.sock"
        # Unix socket paths are limited (104 bytes on macOS): a deep root falls back to the
        # system temp dir; the path is still unique to this controller and recorded.
        self.socket = self.directory / name
        if len(str(self.socket).encode()) > 96:
            self.socket = Path(tempfile.gettempdir()) / f"vader-{name}"
        self.server_pid = None
        self.server_started = None

    def _run(self, *args, check=True):
        if self.tmux is None:
            raise TmuxUnavailable("tmux not found on PATH")
        try:
            done = self.runner(
                [self.tmux, "-S", str(self.socket), *args],
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxUnavailable(f"tmux {args[0]}: {type(exc).__name__}") from None
        if check and done.returncode != 0:
            raise TmuxUnavailable(f"tmux {args[0]}: {done.stderr.strip()[:200]}")
        return done.stdout

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self._run(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            self.session,
            "-n",
            "controller",
            "-c",
            str(self.directory),
            "tail -f /dev/null",
        )
        pid, started = (
            self._run("display-message", "-p", "-t", self.session, "#{pid}\t#{start_time}")
            .strip()
            .split("\t")
        )
        self.server_pid, self.server_started = pid, started
        return {"socket_path": str(self.socket), "server_pid": pid, "server_started": started}

    def alive(self):
        try:
            return (
                self._run("display-message", "-p", "-t", self.session, "#{pid}").strip()
                == self.server_pid
            )
        except TmuxUnavailable:
            return False

    def open_pane(self, name, cwd, command):
        """A new window running `command` in `cwd`; returns pane id and pane pid."""
        Path(cwd).mkdir(parents=True, exist_ok=True)
        out = self._run(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}\t#{pane_pid}",
            "-t",
            self.session,
            "-n",
            name[:40],
            "-c",
            str(cwd),
            command,
        )
        pane_id, pane_pid = out.strip().split("\t")
        return {"pane_id": pane_id, "pane_pid": pane_pid}

    def set_identity(self, pane_id, worker_id, task_id, run_id):
        token = secrets.token_hex(16)
        for option, value in zip(OPTIONS, (worker_id, task_id, run_id, token)):
            self._run("set-option", "-p", "-t", pane_id, option, value)
        return token

    def pane_alive(self, pane_id):
        try:
            return (
                pane_id
                in self._run("list-panes", "-s", "-t", self.session, "-F", "#{pane_id}").split()
            )
        except TmuxUnavailable:
            return False

    def kill(self):
        try:
            self._run("kill-server", check=False)
        except TmuxUnavailable:
            pass
        try:
            self.socket.unlink()
        except OSError:
            pass
