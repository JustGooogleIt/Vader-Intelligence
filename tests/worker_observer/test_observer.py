"""Synthetic panes/reports only; no tmux server, agent or provider calls."""

import copy
import hashlib
import json
import os
import signal
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.worker_observer.boundary import Unavailable, decode, run, workspace_bytes
from tools.worker_observer.observer import Observer, configuration, preview

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path):
    return {
        "version": 1,
        "upstream_python": sys.executable,
        "tmux": str(tmp_path / "tmux"),
        "workspace": str(tmp_path),
        "socket_path": str(tmp_path / "test.sock"),
        "host": socket.gethostname(),
        "server_pid": "100",
        "server_started": "1790760000",
        "allow_external": False,
        "workers": [
            {
                "worker_id": "synthetic-worker",
                "role": "test",
                "pane_id": "%7",
                "pane_pid": "101",
                "pane_token": "synthetic-nonce",
                "checkout": str(tmp_path),
                "task_id": "synthetic-task",
                "run_id": "synthetic-run",
            }
        ],
    }


class Fake:
    def __init__(self, config):
        self.c = config
        self.calls = []
        self.terminal = 'echo "tests passed"\n152 passed\n'
        self.replacement = False
        self.checks = 0
        self.error = None
        self.advice = {
            "choice": "done",
            "confidence": 0.99,
            "probabilities": {
                "done": 1,
                "running": 0,
                "awaiting_input": 0,
                "failed": 0,
                "unclear": 0,
            },
            "completion_verified": True,
            "authorization_granted": True,
        }

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise Unavailable(self.error)
        w = self.c["workers"][0]
        if "display-message" in argv:
            self.checks += 1
            values = [
                self.c["server_pid"],
                self.c["server_started"],
                self.c["socket_path"],
                w["pane_id"],
                w["pane_pid"],
                w["worker_id"],
                w["task_id"],
                w["run_id"],
                w["pane_token"],
            ]
            if self.replacement and self.checks > 1:
                values[-1] = "replacement"
            return "\x1f".join(values) + "\n"
        command = argv[3]
        info = {
            "pane_id": "%7",
            "session_name": "synthetic",
            "window_name": "test",
            "pane_current_command": "python",
            "pane_current_path": w["checkout"],
            "pane_title": "untrusted title",
        }
        if command == "panes":
            return json.dumps(
                [info, {"pane_id": "%88", "pane_title": "UNRELATED PRIVATE METADATA"}]
            )
        if command == "info":
            return json.dumps(info)
        if command == "capture":
            return self.terminal
        if command == "assessment":
            return json.dumps(self.advice)
        raise AssertionError(argv)


@pytest.fixture
def setup(config):
    fake = Fake(config)
    return Observer(config, fake), fake


def test_local_capture_never_infers_completion_or_exports_other_panes(setup):
    o, fake = setup
    result = o.inspect("synthetic-worker", now=NOW)
    assert result["identity"] == "matched"
    assert result["terminal"] == fake.terminal
    assert result["completion_verified"] is False
    assert "UNRELATED" not in json.dumps(result)
    assert not any("assessment" in argv for argv, _ in fake.calls)
    assert all(
        "JEV_API_KEY" not in kwargs["env"]
        for argv, kwargs in fake.calls
        if "upstream.py" in " ".join(argv)
    )


@pytest.mark.parametrize(
    "reason", ["executable_unavailable", "command_failed", "timeout", "output_limit"]
)
def test_missing_tmux_server_timeout_and_overflow(setup, reason):
    o, fake = setup
    fake.error = reason
    result = o.inspect("synthetic-worker")
    assert result["identity"] == "unknown" and result["reason"] == reason
    assert result["terminal"] is None


@pytest.mark.parametrize("response", ["not-json", "{}", '[{"pane_id":"%8"}]', "[1]", "NaN"])
def test_malformed_or_missing_pane(config, response):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return response

    result = Observer(config, runner).inspect("synthetic-worker")
    assert result["identity"] == "unknown"
    assert len(calls) == 1


def test_replacement_discards_capture(setup):
    o, fake = setup
    fake.replacement = True
    result = o.inspect("synthetic-worker")
    assert result["reason"] == "identity_mismatch"
    assert result["terminal"] is None


@pytest.mark.parametrize("change", ["outside", "subdirectory", "missing", "relative"])
def test_directory_change_during_capture_discards_text_and_advice(config, tmp_path, change):
    fake = Fake(config)
    config["allow_external"] = True
    outside = tmp_path.parent
    subdirectory = tmp_path / "subdirectory"
    subdirectory.mkdir()
    paths = {
        "outside": str(outside),
        "subdirectory": str(subdirectory),
        "missing": str(tmp_path / "missing"),
        "relative": ".",
    }
    captured = False

    def runner(argv, **kwargs):
        nonlocal captured
        value = fake(argv, **kwargs)
        if "capture" in argv:
            captured = True
        if "info" in argv and captured:
            info = json.loads(value)
            info["pane_current_path"] = paths[change]
            return json.dumps(info)
        return value

    observer = Observer(config, runner)
    result = observer.inspect("synthetic-worker")
    assert captured  # Original failure: same identity, different cwd after capture.
    assert result["identity"] == "unknown"
    assert result["terminal"] is None
    assert fake.terminal not in json.dumps(result)
    assert observer.assess(result, opt_in=True)["state"] == "unavailable"
    assert not any("assessment" in argv for argv, _ in fake.calls)


def test_post_capture_directory_uses_canonical_path(setup):
    observer, fake = setup

    def runner(argv, **kwargs):
        value = fake(argv, **kwargs)
        if "info" in argv:
            info = json.loads(value)
            info["pane_current_path"] += "/."
            return json.dumps(info)
        return value

    observer.run = runner
    assert observer.inspect("synthetic-worker")["terminal"] == fake.terminal


@pytest.mark.skipif(os.name != "posix", reason="native POSIX symlink replacement")
def test_checkout_symlink_change_during_capture(config, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    link = tmp_path / "checkout"
    link.symlink_to(allowed, target_is_directory=True)
    config["workers"][0]["checkout"] = str(link)
    fake = Fake(config)

    def runner(argv, **kwargs):
        value = fake(argv, **kwargs)
        if "capture" in argv:
            link.unlink()
            link.symlink_to(tmp_path.parent, target_is_directory=True)
        return value

    result = Observer(config, runner).inspect("synthetic-worker")
    assert result["reason"] == "directory_mismatch"
    assert result["terminal"] is None


@pytest.mark.parametrize("failure", [False, True])
def test_subprocess_restores_previous_signal_handlers(failure):
    def prior_handler(_signum, _frame):
        raise AssertionError("no signal sent by this portable test")

    saved = {s: signal.signal(s, prior_handler) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        if failure:
            with pytest.raises(Unavailable, match="executable_unavailable"):
                run(["nonexistent-vader-observer-test-executable"])
        else:
            assert run([sys.executable, "-c", "print('ok')"]).strip() == "ok"
        assert all(signal.getsignal(s) is prior_handler for s in saved)
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


@pytest.mark.parametrize("index", range(9))
def test_every_identity_field_enforced(config, index):
    fake = Fake(config)

    def runner(argv, **kwargs):
        value = fake(argv, **kwargs)
        if "display-message" in argv:
            parts = value.rstrip("\n").split("\x1f")
            parts[index] = "changed"
            return "\x1f".join(parts)
        return value

    result = Observer(config, runner).inspect("synthetic-worker")
    assert result["reason"] == "identity_mismatch"
    assert not any("capture" in argv for argv, _ in fake.calls)


def test_host_mismatch_no_commands(config):
    config["host"] = "other-host.invalid"
    o, fake = Observer(config, Fake(config)), Fake(config)
    o.run = fake
    assert o.inspect("synthetic-worker")["reason"] == "host_mismatch"
    assert not fake.calls


def test_discover_does_not_capture(setup):
    o, fake = setup
    assert o.inspect("synthetic-worker", capture=False)["terminal"] is None
    assert not any("capture" in argv for argv, _ in fake.calls)


def report(config):
    content = b"synthetic acceptance report; not independent evidence\n"
    Path(config["workspace"], "acceptance.txt").write_bytes(content)
    return {
        "version": 1,
        "worker_id": "synthetic-worker",
        "task_id": "synthetic-task",
        "run_id": "synthetic-run",
        "updated_at": NOW.isoformat(),
        "input_delivery": "reported_delivered",
        "acknowledged": True,
        "reported_completion": True,
        "commit": "0" * 40,
        "command": {"argv": ["echo", "tests passed"], "exit_code": 0},
        "artifacts": [{"path": "acceptance.txt", "sha256": hashlib.sha256(content).hexdigest()}],
    }


def load_report(o, data):
    Path(o.config["workspace"], "status.json").write_text(json.dumps(data), encoding="utf-8")
    return o.status(o.inspect("synthetic-worker", now=NOW), "status.json", NOW)


def test_valid_status_is_only_a_report(setup):
    o, _ = setup
    result = load_report(o, report(o.config))
    assert result["valid"] and result["reported_completion"]
    assert result["input_delivery"] == "reported_delivered"
    assert result["acknowledgement"] == "reported"
    assert result["completion_verified"] is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("task_id", "other"),
        ("run_id", "old"),
        ("worker_id", "other"),
        ("version", 2),
        ("acknowledged", "yes"),
        ("commit", "not-a-sha"),
    ],
)
def test_invalid_report_identity_and_schema(setup, key, value):
    o, _ = setup
    data = report(o.config)
    data[key] = value
    assert not load_report(o, data)["valid"]


@pytest.mark.parametrize("seconds,valid", [(300, True), (301, False), (-1, False)])
def test_status_time_boundaries(setup, seconds, valid):
    o, _ = setup
    data = report(o.config)
    data["updated_at"] = (NOW - timedelta(seconds=seconds)).isoformat()
    assert load_report(o, data)["valid"] is valid


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "C:/outside", "a\\..\\outside"])
def test_artifact_traversal(setup, path):
    o, _ = setup
    data = report(o.config)
    data["artifacts"][0]["path"] = path
    assert not load_report(o, data)["valid"]


def test_artifact_hash_mismatch(setup):
    o, _ = setup
    data = report(o.config)
    data["artifacts"][0]["sha256"] = "0" * 64
    assert load_report(o, data)["reason"] == "artifact_hash_mismatch"


def test_external_disabled_even_when_opted_in(setup):
    o, fake = setup
    obs = o.inspect("synthetic-worker")
    assert o.assess(obs, opt_in=True)["state"] == "disabled"
    assert not any("assessment" in a for a, _ in fake.calls)


def test_preview_required_and_done_has_no_authority(setup):
    o, fake = setup
    o.config["allow_external"] = True
    obs = o.inspect("synthetic-worker")
    assert o.assess(obs)["state"] == "disabled"
    assert o.assess(obs, opt_in=True)["state"] == "preview_required"
    payload = preview(obs)
    result = o.assess(obs, opt_in=True, approved_digest=hashlib.sha256(payload).hexdigest())
    assert result["choice"] == "done"
    assert result["completion_verified"] is result["authorization_granted"] is False
    assert fake.calls[-1][1]["payload"] == payload
    assert fake.calls[-1][0][-1] == "assessment"
    assert "send" not in str(fake.calls)


def test_local_report_preferred_without_provider_call(setup):
    o, fake = setup
    o.config["allow_external"] = True
    obs = o.inspect("synthetic-worker")
    obs["local_status"] = load_report(o, report(o.config))
    assert o.assess(obs, opt_in=True)["state"] == "local_status_preferred"
    assert not any("assessment" in a for a, _ in fake.calls)


@pytest.mark.parametrize("bad", [None, {"choice": "done"}, {"choice": []}])
def test_provider_malformed_falls_back(setup, bad):
    o, fake = setup
    o.config["allow_external"] = True
    obs = o.inspect("synthetic-worker")
    fake.advice = bad
    result = o.assess(obs, opt_in=True, approved_digest=hashlib.sha256(preview(obs)).hexdigest())
    assert result["state"] == "provider_unavailable"
    assert obs["terminal"] == fake.terminal


def test_provider_timeout_falls_back(setup):
    o, fake = setup
    o.config["allow_external"] = True
    obs = o.inspect("synthetic-worker")
    fake.error = "timeout"
    assert (
        o.assess(obs, opt_in=True, approved_digest=hashlib.sha256(preview(obs)).hexdigest())[
            "state"
        ]
        == "provider_unavailable"
    )
    assert obs["identity"] == "matched"


def test_config_duplicates_and_unregistered_worker(setup):
    o, _ = setup
    with pytest.raises(Unavailable, match="unregistered"):
        o.inspect("someone-else")
    duplicate = copy.deepcopy(o.config)
    duplicate["workers"].append(duplicate["workers"][0])
    with pytest.raises(Unavailable, match="duplicate"):
        configuration(duplicate)


def test_real_child_argument_boundaries_and_no_shell():
    arg = 'space ; $(never-execute) " quoted'
    out = run([sys.executable, "-c", "import sys;print(sys.argv[1])", arg])
    assert out.strip() == arg


@pytest.mark.parametrize(
    "script,reason",
    [
        ("import time;time.sleep(10)", "timeout"),
        ("import sys;sys.stdout.write('x'*1000000)", "output_limit"),
        ("raise SystemExit(4)", "command_failed"),
    ],
)
def test_real_child_bounds(script, reason):
    with pytest.raises(Unavailable, match=reason):
        run([sys.executable, "-c", script], timeout=0.5, limit=1000)


def test_missing_executable():
    with pytest.raises(Unavailable, match="executable_unavailable"):
        run(["nonexistent-vader-observer-test-executable"])


def test_oversized_artifact(config):
    Path(config["workspace"], "large").write_bytes(b"x" * 65537)
    with pytest.raises(Unavailable):
        workspace_bytes(config["workspace"], "large")


def test_json_limits():
    with pytest.raises(Unavailable):
        decode('{"untrusted":NaN}')


def test_hardlink_artifact_rejected(config):
    import os

    root = Path(config["workspace"])
    (root / "original").write_text("private")
    os.link(root / "original", root / "linked")
    with pytest.raises(Unavailable):
        workspace_bytes(root, "linked")


def test_changed_snapshot_requires_new_consent(setup):
    o, fake = setup
    o.config["allow_external"] = True
    obs = o.inspect("synthetic-worker")
    digest = hashlib.sha256(preview(obs)).hexdigest()
    obs["terminal"] += "changed after preview"
    assert o.assess(obs, opt_in=True, approved_digest=digest)["state"] == "preview_required"
    assert not any("assessment" in a for a, _ in fake.calls)


def test_status_is_not_exported(setup):
    o, _ = setup
    obs = o.inspect("synthetic-worker")
    obs["local_status"] = {"secret": "NEVER EXPORT ARTIFACT CONTENT"}
    assert b"NEVER EXPORT" not in preview(obs)


def test_cli_local_path(setup, monkeypatch, capsys, tmp_path):
    from tools.worker_observer import __main__ as cli

    o, fake = setup
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(o.config))
    monkeypatch.setattr(cli, "Observer", lambda config: o)
    monkeypatch.setattr(
        sys, "argv", ["observer", "--config", str(path), "--worker", "synthetic-worker", "inspect"]
    )
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["advisory"]["state"] == "disabled"
    assert result["terminal"] == fake.terminal


def test_upstream_pin_guard_and_cli_help():
    bridge = Path(__file__).resolve().parents[2] / "tools/worker_observer/upstream.py"
    # Isolated installation at the actual pin; this only invokes argparse help.
    out = run([sys.executable, "-I", str(bridge), "panes", "--help"])
    assert "tmux-jev panes" in out
    with pytest.raises(Unavailable, match="command_failed"):
        run([sys.executable, "-I", str(bridge), "send", "--help"])


def test_bridge_assessment_uses_frozen_input_with_mock_provider(monkeypatch, capsys):
    import io

    from tmux_jev import jev

    from tools.worker_observer import upstream

    state = {"terminal": "anonymous: 197 passed", "task_id": "synthetic", "run_id": "1"}
    seen = []

    def ask(payload, questions):
        seen.append((payload, questions))
        return {
            "status": {
                "choice": "done",
                "confidence": 0.9,
                "probabilities": {
                    "done": 1,
                    "running": 0,
                    "awaiting_input": 0,
                    "failed": 0,
                    "unclear": 0,
                },
            }
        }

    monkeypatch.setattr(jev, "ask", ask)
    monkeypatch.setattr(sys, "argv", ["bridge", "assessment"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(state).encode())))
    assert upstream.main() == 0
    assert seen[0][0] == state
    assert json.loads(capsys.readouterr().out)["choice"] == "done"


def test_bridge_rejects_wrong_install_pin(monkeypatch):
    from tools.worker_observer import upstream

    class WrongDistribution:
        def read_text(self, name):
            return '{"vcs_info":{"commit_id":"wrong"}}'

    monkeypatch.setattr(upstream.importlib.metadata, "distribution", lambda _: WrongDistribution())
    monkeypatch.setattr(sys, "argv", ["bridge", "panes"])
    assert upstream.main() == 1


def test_cli_interactive_preview_sends_only_approved_snapshot(setup, monkeypatch, capsys, tmp_path):
    import io

    from tools.worker_observer import __main__ as cli

    class TerminalInput(io.StringIO):
        def isatty(self):
            return True

    o, fake = setup
    o.config["allow_external"] = True
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(o.config))
    monkeypatch.setattr(cli, "Observer", lambda config: o)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "observer",
            "--config",
            str(config_path),
            "--worker",
            "synthetic-worker",
            "--assess",
            "inspect",
        ],
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput("SEND\n"))
    assert cli.main() == 0
    captured = capsys.readouterr()
    payload = fake.calls[-1][1]["payload"]
    assert payload.decode() in captured.err
    result = json.loads(captured.out)
    assert result["advisory"]["choice"] == "done"
    assert not result["completion_verified"]


def test_committed_synthetic_status_example(setup):
    o, _ = setup
    root = Path(__file__).resolve().parents[2]
    relative = "tools/worker_observer/examples/synthetic-acceptance.txt"
    target = Path(o.config["workspace"], relative)
    target.parent.mkdir(parents=True)
    target.write_bytes((root / relative).read_bytes())
    data = json.loads((root / "tools/worker_observer/examples/synthetic-status.json").read_text())
    result = load_report(o, data)
    assert result["valid"]
    assert result["completion_verified"] is False
