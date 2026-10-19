"""Optional tmux helper: never required, observation only; no tmux server is started."""

import subprocess

from vader_intelligence.pods import tmux


class Done:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_tmux_is_optional_and_observation_only(pods):
    assert tmux.open_session(pods.store, which=lambda _: None) == {
        "available": False,
        "created": False,
        "reason": "tmux not found on PATH",
    }
    assert tmux.close_session(which=lambda _: None)["available"] is False
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        return Done()

    attempts = [{"log_path": "/tmp/a b/worker.log"}]
    planned = tmux.plan_session(pods.root, attempts, tmux="/usr/bin/tmux")
    assert all(argv[:3] == ["/usr/bin/tmux", "-L", "vi-pods"] for argv in planned)
    assert any("tail -n 50 -F '/tmp/a b/worker.log'" in argv[-1] for argv in planned)
    assert "status --root" in planned[0][-1]
    opened = tmux.open_session(pods.store, which=lambda _: "/usr/bin/tmux", runner=runner)
    assert opened["created"] is True and opened["panes"] == 1 and len(commands) == 1

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    failed = tmux.open_session(pods.store, which=lambda _: "/usr/bin/tmux", runner=timeout)
    assert failed == {"available": True, "created": False, "reason": "TimeoutExpired"}
