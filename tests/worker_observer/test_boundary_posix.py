"""Real POSIX signals/processes only; no tmux, providers or production state."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires native POSIX signals/waitid")
ROOT = Path(__file__).resolve().parents[2]


def wait_file(path, process, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        assert process.poll() is None, process.communicate(timeout=1)
        time.sleep(0.01)
    pytest.fail("synthetic child readiness timeout")


def not_running(pid):
    # Orphan grandchildren can briefly be zombies pending the OS reaper. The
    # wrapper must reap its direct child itself (separately asserted below).
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=2
    )
    return status.returncode == 1 or status.stdout.strip().startswith("Z")


@pytest.mark.parametrize("mode", ["running", "stubborn", "creating", "leader_exits"])
def test_sigterm_cleans_owned_group_and_reaps_direct_child(tmp_path, mode):
    ready = tmp_path / "ready.json"
    grand_ready = tmp_path / "grand.ready"
    report = tmp_path / "report.json"
    grand_code = (
        "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(grand_ready)!r}).touch(); time.sleep(10)"
    )
    child_code = f"""
import json, os, pathlib, signal, subprocess, sys, time
if {mode!r} != 'running':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
grand = subprocess.Popen([sys.executable, '-c', {grand_code!r}])
deadline = time.monotonic() + 4
while not pathlib.Path({str(grand_ready)!r}).exists():
    if time.monotonic() >= deadline: raise RuntimeError('grandchild not ready')
    time.sleep(.01)
pathlib.Path({str(ready)!r}).write_text(json.dumps([os.getpid(), grand.pid]))
if {mode!r} != 'leader_exits': time.sleep(10)
"""
    wrapper_code = f"""
import json, os, pathlib, signal, subprocess, sys, time
from tools.worker_observer import boundary
previous = {{s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}}
real_popen = subprocess.Popen
children = []
class Launch(real_popen):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        children.append(self)
        if {mode!r} == 'creating':
            deadline = time.monotonic() + 4
            while not pathlib.Path({str(ready)!r}).exists():
                if time.monotonic() >= deadline: raise RuntimeError('child not ready')
                time.sleep(.01)
            # A real kernel signal before run() receives the child handle.
            os.kill(os.getpid(), signal.SIGTERM)
subprocess.Popen = Launch
code = 0
try:
    boundary.run([sys.executable, '-c', {child_code!r}], timeout=8)
except SystemExit as exc:
    code = exc.code
except boundary.Unavailable:
    code = 1
reaped = []
for child in children:
    try: os.waitpid(child.pid, os.WNOHANG)
    except ChildProcessError: reaped.append(True)
    else: reaped.append(False)
pathlib.Path({str(report)!r}).write_text(json.dumps(dict(
    reaped=reaped, restored=all(signal.getsignal(s) == h for s,h in previous.items()),
    codes=[c.returncode for c in children])))
sys.exit(code)
"""
    # Sentinel is deliberately in a different owned session and must survive.
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(10)"], start_new_session=True
    )
    wrapper = subprocess.Popen(
        [sys.executable, "-c", wrapper_code],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        wait_file(ready, wrapper)
        if mode != "creating":
            if mode == "leader_exits":
                time.sleep(0.1)  # Leader exits; descendant still holds stdout.
            wrapper.send_signal(signal.SIGTERM)
            time.sleep(0.05)
            wrapper.send_signal(signal.SIGTERM)  # Must not interrupt escalation/reaping.
        out, err = wrapper.communicate(timeout=6)
        assert wrapper.returncode == 143, (out, err)
        result = json.loads(report.read_text())
        assert result["restored"] and result["reaped"] == [True]
        if mode in ("stubborn", "creating"):
            assert result["codes"] == [-signal.SIGKILL]
        direct, grand = json.loads(ready.read_text())
        assert not_running(direct)
        deadline = time.monotonic() + 2
        while not not_running(grand) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not_running(grand)
        assert sentinel.poll() is None
    finally:
        # Only Popen-owned direct processes. Synthetic descendants self-expire
        # after 10s even when reproducing the OLD orphan bug; no PID-file kills.
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.wait(timeout=2)
        sentinel.terminate()
        sentinel.wait(timeout=2)


def test_auto_reaping_handler_refused_before_spawn():
    from tools.worker_observer.boundary import Unavailable, run

    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(Unavailable, match="exclusive_child_wait_required"):
            run([sys.executable, "-c", "raise AssertionError('must not launch')"])
    finally:
        signal.signal(signal.SIGCHLD, previous)


def test_completed_leader_kept_owned_until_descendant_cleanup(tmp_path):
    from tools.worker_observer.boundary import run

    ready = tmp_path / "grand.ready"
    grand_code = (
        "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(10)"
    )
    child_code = f"""
import pathlib, subprocess, sys, time
grand = subprocess.Popen([sys.executable, '-c', {grand_code!r}],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 4
while not pathlib.Path({str(ready)!r}).exists():
    if time.monotonic() >= deadline: raise RuntimeError('grandchild not ready')
    time.sleep(.01)
print(grand.pid)
"""
    grand = int(run([sys.executable, "-c", child_code]))
    deadline = time.monotonic() + 2
    while not not_running(grand) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not_running(grand)
