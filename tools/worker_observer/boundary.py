"""Bounded subprocess and workspace-file boundaries."""

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class Unavailable(ValueError):
    """A safe, fixed reason code; never include captured stderr or credentials."""


def decode(raw):
    try:
        return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise Unavailable("malformed_json") from None


@contextmanager
def termination_request():
    """Defer SIGTERM/INT until Popen has returned and cleanup has finished."""
    if threading.current_thread() is not threading.main_thread():
        raise Unavailable("main_thread_required")
    requested = []
    previous = {}

    def request(signum, _frame):
        if not requested:
            requested.append(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, request)
        yield requested
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if requested:
            raise SystemExit(128 + requested[0])


def cleanup(child):
    failure = None
    if os.name == "posix":
        # The direct child is our session/group leader. Never poll/wait it before
        # the final group signal, even if it unexpectedly exits. Exclusive wait
        # ownership and default SIGCHLD reserve its PID until our wait below.
        if child.returncode is not None:
            raise Unavailable("child_ownership_lost")
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass
            except OSError:
                failure = "cleanup_signal_failed"
            if signum == signal.SIGTERM:
                time.sleep(0.25)
    elif child.poll() is None:
        child.kill()
    # Reap even when signaling failed; never silently accept a permission error.
    child.wait(timeout=2)
    if failure:
        raise Unavailable(failure)


def run(argv, *, env=None, timeout=5, limit=131072, payload=None):
    """No shell. Retain <=limit+1 bytes, kill on overflow/deadline, reap child.

    POSIX children get a process group so upstream's nested tmux is also bounded.
    Windows tests use ordinary direct children; real tmux is a Unix requirement.
    """
    if os.name == "posix" and signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise Unavailable("exclusive_child_wait_required")
    with termination_request() as requested, tempfile.TemporaryFile() as source:
        source.write(payload or b"")
        source.seek(0)
        child = None
        status_read = status_write = None
        status = bytearray()
        command_code = None
        reader = None
        reason = None
        data = bytearray()
        finished = threading.Event()
        read_failed = threading.Event()

        def drain():
            try:
                while len(data) <= limit:
                    chunk = child.stdout.read(min(4096, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
            except OSError:
                read_failed.set()
            finally:
                finished.set()

        try:
            if requested:
                return  # termination_request raises after restoring handlers
            launch = argv
            extra = {}
            if os.name == "posix":
                status_read, status_write = os.pipe()
                os.set_blocking(status_read, False)
                launch = [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("owned_child.py")),
                    str(status_write),
                    *argv,
                ]
                extra["pass_fds"] = (status_write,)
            child = subprocess.Popen(
                launch,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=os.name == "posix",
                **extra,
            )
            if status_write is not None:
                os.close(status_write)
                status_write = None
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            while not requested:
                if status_read is not None and command_code is None:
                    try:
                        chunk = os.read(status_read, 65 - len(status))
                    except BlockingIOError:
                        chunk = None
                    if chunk == b"":
                        reason = "child_supervisor_failed"
                        break
                    if chunk:
                        status.extend(chunk)
                        if len(status) > 64:
                            reason = "child_supervisor_failed"
                            break
                        if status.endswith(b"\n"):
                            if status == b"launch_error\n":
                                reason = "executable_unavailable"
                                break
                            try:
                                prefix, code = status.decode("ascii").strip().split(":")
                                if prefix != "exit":
                                    raise ValueError()
                                command_code = int(code)
                            except (ValueError, UnicodeError):
                                reason = "child_supervisor_failed"
                                break
                if finished.is_set() and (
                    command_code is not None if os.name == "posix" else child.poll() is not None
                ):
                    break
                if len(data) > limit:
                    reason = "output_limit"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                time.sleep(0.01)
        except OSError:
            raise Unavailable("executable_unavailable") from None
        finally:
            try:
                if child is not None:
                    try:
                        cleanup(child)
                    except subprocess.TimeoutExpired:
                        reason = "cleanup_timeout"
                    finally:
                        if reader is not None and reader.ident is not None:
                            reader.join(timeout=2)
                        if reader is None or not reader.is_alive():
                            child.stdout.close()
            finally:
                for fd in (status_read, status_write):
                    if fd is not None:
                        os.close(fd)
        if reason or len(data) > limit:
            raise Unavailable(reason or "output_limit")
        if reader.is_alive():
            raise Unavailable("pipe_not_closed")
        if read_failed.is_set():
            raise Unavailable("pipe_read_failed")
        returncode = command_code if os.name == "posix" else child.returncode
        if returncode:
            raise Unavailable("command_failed")
        try:
            return data.decode("utf-8")
        except UnicodeError:
            raise Unavailable("invalid_utf8") from None


def workspace_bytes(workspace, relative, limit=65536):
    """Read a regular file under a trusted workspace; refuse links and traversal.

    POSIX uses descriptor-relative no-follow traversal. Windows validation supports
    fixtures only and assumes the configured workspace is not concurrently hostile.
    Hard links are rejected too. File contents remain untrusted, even after hashing.
    """
    root = Path(workspace).resolve(strict=True)
    path = Path(relative)
    if (
        not isinstance(relative, str)
        or not relative
        or path.is_absolute()
        or any(p in ("..", ".") for p in path.parts)
        or ":" in relative
        or "\\" in relative
    ):
        raise Unavailable("unsafe_artifact_path")
    handles = []
    try:
        if os.name == "posix":
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            handles.append(fd)
            for part in path.parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                handles.append(fd)
            fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        else:
            target = root
            for part in path.parts:
                target = target / part
                if target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
                    raise Unavailable("unsafe_artifact_path")
            target.resolve(strict=True).relative_to(root)
            fd = os.open(target, os.O_RDONLY | os.O_BINARY)
        handles.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise Unavailable("artifact_not_bounded_regular_file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise Unavailable("artifact_too_large")
        return value
    except (OSError, ValueError, IndexError):
        raise Unavailable("artifact_unavailable_or_unsafe") from None
    finally:
        for fd in reversed(handles):
            os.close(fd)
