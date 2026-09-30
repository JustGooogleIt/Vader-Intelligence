"""Bounded subprocess and workspace-file boundaries."""

import json
import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path


class Unavailable(ValueError):
    """A safe, fixed reason code; never include captured stderr or credentials."""


def decode(raw):
    try:
        return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise Unavailable("malformed_json") from None


def run(argv, *, env=None, timeout=5, limit=131072, payload=None):
    """No shell. Retain <=limit+1 bytes, kill on overflow/deadline, reap child.

    POSIX children get a process group so upstream's nested tmux is also bounded.
    Windows tests use ordinary direct children; real tmux is a Unix requirement.
    """
    with tempfile.TemporaryFile() as source:
        source.write(payload or b"")
        source.seek(0)
        try:
            child = subprocess.Popen(
                argv,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=os.name == "posix",
            )
        except OSError:
            raise Unavailable("executable_unavailable") from None
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

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        reason = None
        try:
            while not (finished.is_set() and child.poll() is not None):
                if len(data) > limit:
                    reason = "output_limit"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                time.sleep(0.01)
        finally:
            if os.name == "posix":
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                reason = "cleanup_timeout"
            reader.join(timeout=2)
            if not reader.is_alive():
                child.stdout.close()
        if reason or len(data) > limit:
            raise Unavailable(reason or "output_limit")
        if reader.is_alive():
            raise Unavailable("pipe_not_closed")
        if read_failed.is_set():
            raise Unavailable("pipe_read_failed")
        if child.returncode:
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
