"""J4: the bounded coding-improvement loop on a THROWAWAY git repository (never this
checkout): frozen brief, isolated writer worktree/branch, trusted checks on the exact
commit, independent reviewer, review packet, accept/reject; policy and budget bounds;
real agent executors stay disabled without an authorization record."""

import json
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from vader_intelligence.command_center import projections
from vader_intelligence.command_center.coding import agent_argv, validate_brief
from vader_intelligence.command_center.config import CenterConfig
from vader_intelligence.command_center.controller import Controller
from vader_intelligence.command_center.store import CenterStore
from vader_intelligence.pods.coordinator import CoordinatorConfig

FAST = CoordinatorConfig(
    heartbeat_seconds=0.1,
    lease_seconds=2.0,
    cancel_grace_seconds=0.3,
    backoff_base_seconds=0.05,
    backoff_max_seconds=0.2,
    poll_seconds=0.02,
    kill_wait_seconds=5.0,
)


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x.invalid",
        },
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A tiny project with one real defect: `clamp` ignores the upper bound."""
    repo = tmp_path / "repo"
    (repo / "src" / "tiny").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "docs" / "evidence").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length = 100\n[tool.pytest.ini_options]\npythonpath = ["src"]\n'
    )
    (repo / "src" / "tiny" / "__init__.py").write_text("")
    (repo / "src" / "tiny" / "mod.py").write_text(
        "def clamp(value, low, high):\n    return max(low, value)\n"
    )
    (repo / "tests" / "test_mod.py").write_text(
        "from tiny.mod import clamp\n\n\ndef test_low():\n    assert clamp(-1, 0, 10) == 0\n"
    )
    (repo / "docs" / "evidence" / "record.md").write_text("frozen\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


GOOD_PATCH = textwrap.dedent(
    """\
    diff --git a/src/tiny/mod.py b/src/tiny/mod.py
    --- a/src/tiny/mod.py
    +++ b/src/tiny/mod.py
    @@ -1,2 +1,2 @@
     def clamp(value, low, high):
    -    return max(low, value)
    +    return min(high, max(low, value))
    diff --git a/tests/test_mod.py b/tests/test_mod.py
    --- a/tests/test_mod.py
    +++ b/tests/test_mod.py
    @@ -3,3 +3,7 @@
    
     def test_low():
         assert clamp(-1, 0, 10) == 0
    +
    +
    +def test_high():
    +    assert clamp(99, 0, 10) == 10
    """
)
BAD_PATCH = GOOD_PATCH.replace(
    "+    return min(high, max(low, value))", "+    return min(high, max(low, value)) + 1"
)
PROTECTED_PATCH = textwrap.dedent(
    """\
    diff --git a/docs/evidence/record.md b/docs/evidence/record.md
    --- a/docs/evidence/record.md
    +++ b/docs/evidence/record.md
    @@ -1 +1 @@
    -frozen
    +rewritten
    """
)
DELETE_TEST_PATCH = textwrap.dedent(
    """\
    diff --git a/tests/test_mod.py b/tests/test_mod.py
    deleted file mode 100644
    --- a/tests/test_mod.py
    +++ /dev/null
    @@ -1,5 +0,0 @@
    -from tiny.mod import clamp
    -
    -
    -def test_low():
    -    assert clamp(-1, 0, 10) == 0
    """
)


def brief(patch, **extra):
    return {
        "kind": "maintenance",
        "issue": "clamp ignores the upper bound",
        "description": "clamp(99, 0, 10) returns 99; it must return 10",
        "acceptance_criteria": [
            "clamp(99, 0, 10) == 10",
            "existing tests still pass",
            "ruff clean",
        ],
        "executor": "scripted",
        "patch": patch,
        "checks": [
            {"name": "ruff_check", "paths": ["src", "tests"]},
            {"name": "ruff_format", "paths": ["src", "tests"]},
            {"name": "pytest", "paths": ["tests"]},
        ],
        "limits": {"wall_seconds": 600},
        **extra,
    }


@pytest.fixture
def ctl(cfg, repo):
    cfg = CenterConfig(**{**cfg.__dict__, "repo": repo})
    controller = Controller(cfg, coordinator_config=FAST, tmux=False)
    controller.coding.python = sys.executable
    yield controller
    controller.shutdown()


def drive(ctl, run_id, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ctl.step()
        run = ctl.center.run(run_id)
        if run["state"] in ("completed", "failed", "canceled"):
            return run
        time.sleep(0.1)
    raise AssertionError(
        "run did not finish: " + json.dumps(ctl.center.run(run_id)["detail"])[:500]
    )


def start(ctl, payload):
    cid = str(uuid.uuid4())
    ctl.center.receive_command(cid, "start", None, payload)
    report = ctl.step()
    state = ctl.center.command(cid)
    assert state["state"] == "applied", state["reason"]
    return [c for c in report["commands"] if c[0] == cid][0][2]


def test_one_complete_cycle_on_a_genuine_defect(ctl, cfg, repo):
    base = git(repo, "rev-parse", "HEAD")
    run_id = start(ctl, brief(GOOD_PATCH))
    run = ctl.center.run(run_id)
    assert (
        run["kind"] == "maintenance"
        and run["pins"]["base_commit"] == base
        and run["pins"]["simulated"] is True
    )
    frozen = json.loads((cfg.root / "coding" / run_id / "brief.json").read_text())
    assert (
        frozen["brief_sha256"] == run["pins"]["brief_sha256"]
        and frozen["acceptance_criteria"][0] == "clamp(99, 0, 10) == 10"
    )
    run = drive(ctl, run_id)
    assert run["state"] == "completed", run["detail"]
    d = run["detail"]
    assert run["outcome"] == "accept" and d["recommendation"] == "accept"
    assert d["commit"] != base and d["branch"] == f"cc/{run_id[:8]}/writer-1"
    assert sorted(d["files_changed"]) == ["src/tiny/mod.py", "tests/test_mod.py"]
    assert [c["exit_code"] for c in d["checks"]] == [0, 0, 0] and [
        c["exit_code"] for c in d["reviewer_checks"]
    ] == [0, 0, 0]
    assert all(c["commit"] == d["commit"] for c in d["checks"] + d["reviewer_checks"])
    assert git(Path(d["writer_worktree"]), "rev-parse", "HEAD") == d["commit"]
    assert git(Path(d["reviewer_worktree"]), "rev-parse", "HEAD") == d["commit"]
    assert git(repo, "rev-parse", "HEAD") == base  # nothing merged
    packet = json.loads(Path(d["review"]["packet"]).read_text())
    assert packet["verdict"] == "accept" and packet["reviewer"]["id"] != packet["writer"]["id"]
    assert packet["writer"]["simulated"] is True and "not merged" in packet["integration"]
    assert (cfg.root / "coding" / run_id / "review-packet.md").exists()
    for check in d["checks"]:
        assert Path(check["log"]).exists()
        if check["name"] == "pytest":
            assert "passed" in check["tail"]
        assert "--config" in check["argv"] or "-c" in check["argv"]  # trusted configuration
    eng = projections.engineering(cfg)
    assert (
        eng["state"] == "ok"
        and eng["runs"][0]["run_id"] == run_id
        and eng["runs"][0]["detail"]["review"]["verdict"] == "accept"
    )
    assert projections.farms(cfg)["runs"][0]["kind"] == "maintenance"  # listed, never a game farm


def test_a_patch_that_breaks_checks_is_rejected_and_preserved(ctl, cfg):
    run_id = start(ctl, brief(BAD_PATCH))
    run = drive(ctl, run_id)
    d = run["detail"]
    assert run["state"] == "completed" and run["outcome"] == "reject"
    failing = [c for c in d["reviewer_checks"] if c["name"] == "pytest"]
    assert failing and failing[0]["exit_code"] != 0 and "failed" in failing[0]["tail"]
    assert any(f["kind"] == "checks_failed" for f in d["review"]["findings"])
    assert (
        Path(d["writer_worktree"]).exists() and Path(failing[0]["log"]).exists()
    )  # the failed attempt stays


@pytest.mark.parametrize(
    "patch,kind", [(PROTECTED_PATCH, "protected_path"), (DELETE_TEST_PATCH, "deleted_test")]
)
def test_policy_violations_are_rejected_before_any_check(ctl, patch, kind):
    run_id = start(ctl, brief(patch))
    run = drive(ctl, run_id)
    d = run["detail"]
    assert run["state"] == "completed" and run["outcome"] == "reject"
    kinds = {f["kind"] for f in d["policy_findings"]}
    assert kind in kinds and kinds <= {kind, "fewer_tests"}
    assert "checks" not in d or d["checks"] == []  # no check ran on a disallowed patch


def test_real_agents_stay_disabled_without_an_authorization_record(ctl, cfg):
    run_id = start(
        ctl, {**brief(None), "executor": "claude_cli", "patch": None, "prompt": "fix clamp"}
    )
    run = drive(ctl, run_id)
    d = run["detail"]
    assert run["state"] == "failed" and run["outcome"] == "execution_disabled"
    assert d["executor_argv"][:2] == ["claude", "-p"] and "--max-budget-usd" in d["executor_argv"]
    assert "signed-in CLI is not proof" in d["blocker"]
    assert not any(p.name.startswith("agent") for p in (cfg.root / "coding" / run_id).iterdir())
    argv = agent_argv("codex_cli", "/tmp/wt", "do it", "/tmp/out.json")
    assert (
        argv[:4] == ["codex", "exec", "--sandbox", "workspace-write"]
        and "-a" in argv
        and "never" in argv
    )
    (cfg.root / "authorization.json").write_text(
        json.dumps(
            {
                "usage": "established",
                "incremental_cost_usd": 0,
                "executors": ["codex_cli"],
                "by": "owner",
                "at": "2026-10-23T00:00:00Z",
            }
        )
    )
    run_id = start(
        ctl, {**brief(None), "executor": "codex_cli", "patch": None, "prompt": "fix clamp"}
    )
    run = drive(ctl, run_id)
    assert run["state"] == "failed" and "not exercised in this pilot" in run["detail"]["error"]


def test_budget_writer_cap_cancel_and_invalid_briefs(ctl, cfg):
    for bad in (
        {**brief(GOOD_PATCH), "issue": ""},
        {**brief(GOOD_PATCH), "checks": [{"name": "rm"}]},
        {**brief(GOOD_PATCH), "patch": ""},
        {**brief(GOOD_PATCH), "limits": {"wall_seconds": 5}},
        {**brief(GOOD_PATCH), "checks": [{"name": "pytest", "paths": ["../etc"]}]},
    ):
        with pytest.raises(ValueError):
            validate_brief(bad)
    a = start(ctl, brief(GOOD_PATCH))
    b = start(ctl, brief(BAD_PATCH))
    cid = str(uuid.uuid4())
    ctl.center.receive_command(cid, "start", None, brief(GOOD_PATCH, description="third"))
    ctl.step()
    assert "max_writers" in ctl.center.command(cid)["reason"]
    assert ctl.coding.live_processes() <= 2  # one check process per writer, under the cap
    assert ctl.farms.active_processes() == ctl.coding.live_processes()
    # Cancel a run while its check runs: the process group is gone, evidence stays.
    end = time.monotonic() + 60
    while time.monotonic() < end and b not in ctl.coding.processes:
        ctl.step()
        time.sleep(0.05)
    assert b in ctl.coding.processes
    pid = ctl.coding.processes[b]["popen"].pid
    cancel = str(uuid.uuid4())
    ctl.center.receive_command(cancel, "cancel", b, {})
    ctl.step()
    run_b = ctl.center.run(b)
    assert run_b["state"] == "canceled" and b not in ctl.coding.processes
    with pytest.raises(ProcessLookupError):
        import os

        os.killpg(pid, 0)
    assert Path(run_b["detail"]["writer_worktree"]).exists()
    # Budget: an exhausted wall budget fails the run and kills its process.
    run_a = ctl.center.run(a)
    ctl.center.set_run(a, detail={**run_a["detail"], "started_at": time.time() - 10_000})
    run_a = drive(ctl, a)
    assert run_a["state"] == "failed" and run_a["outcome"] == "budget_exhausted"
    with CenterStore(cfg.root, readonly=True) as center:
        assert {r["state"] for r in center.runs(kind="maintenance")} == {"failed", "canceled"}


CONFTEST_PATCH = BAD_PATCH + textwrap.dedent(
    """\
    diff --git a/conftest.py b/conftest.py
    new file mode 100644
    --- /dev/null
    +++ b/conftest.py
    @@ -0,0 +1,3 @@
    +import os
    +
    +os._exit(0)
    """
)
RENAME_PATCH = textwrap.dedent(
    """\
    diff --git a/tests/test_mod.py b/scratch/not_a_test.py
    similarity index 100%
    rename from tests/test_mod.py
    rename to scratch/not_a_test.py
    diff --git a/tests/test_other.py b/tests/test_other.py
    new file mode 100644
    --- /dev/null
    +++ b/tests/test_other.py
    @@ -0,0 +1,2 @@
    +def test_other():
    +    assert True
    """
)


def test_a_patch_that_steers_the_checks_is_rejected_by_policy(ctl):
    """Internal review H1: a conftest.py (or any check configuration) in the patch is a
    policy finding before any check runs; checks read the base commit's configuration."""
    from vader_intelligence.command_center.coding import DEFAULT_PROTECTED

    run_id = start(ctl, brief(CONFTEST_PATCH))
    run = drive(ctl, run_id)
    d = run["detail"]
    assert run["outcome"] == "reject"
    assert {f["kind"] for f in d["policy_findings"]} >= {"conftest_changed"}
    assert d.get("checks", []) == []
    for name in ("pytest.ini", "ruff.toml", "setup.cfg", "tox.ini"):
        assert name in DEFAULT_PROTECTED


def test_the_protected_floor_cannot_be_removed_by_the_brief(ctl):
    """Internal review H2: `protected_paths` in a brief only adds to the floor."""
    payload = brief(PROTECTED_PATCH, protected_paths=["nothing-here/"])
    frozen, _ = validate_brief(payload)
    assert (
        "docs/evidence/" in frozen["protected_paths"]
        and "nothing-here/" in frozen["protected_paths"]
    )
    run_id = start(ctl, payload)
    run = drive(ctl, run_id)
    assert run["outcome"] == "reject"
    assert {f["kind"] for f in run["detail"]["policy_findings"]} == {"protected_path"}


def test_renamed_or_removed_tests_are_findings(ctl):
    """Internal review M4: moving a test out of tests/ or lowering the test count is caught."""
    run_id = start(ctl, brief(RENAME_PATCH))
    run = drive(ctl, run_id)
    kinds = {f["kind"] for f in run["detail"]["policy_findings"]}
    assert run["outcome"] == "reject" and "deleted_test" in kinds


def test_mistyped_commands_settle_as_rejected_receipts(ctl):
    """Internal review M1: schema-valid but mistyped payloads never crash the controller
    loop; the receipt says why, and the next step proceeds."""
    bad = [
        {**brief(GOOD_PATCH), "limits": 5},
        {**brief(GOOD_PATCH), "checks": 7},
        {"template": [], "game_id": 824948},
        {"template": "evidence_review_v1", "roles": 5, "game_id": 824948},
    ]
    ids = []
    for payload in bad:
        cid = str(uuid.uuid4())
        ctl.center.receive_command(cid, "start", None, payload)
        ids.append(cid)
    ctl.step()
    ctl.step()
    for cid in ids:
        row = ctl.center.command(cid)
        assert row["state"] == "rejected" and row["reason"], row
    assert ctl.center.pending_commands() == []
    assert ctl.center.runs() == []


def test_coding_checks_wait_for_global_capacity(ctl, cfg):
    """Internal review M3: check processes count toward the global cap of four and are not
    spawned while foreign attempts fill it."""
    from .test_farms import fill_capacity

    sleepers = fill_capacity(ctl, seconds=6, count=4)
    end = time.monotonic() + 20
    while time.monotonic() < end and len(ctl.pods.open_attempts()) < 4:
        ctl.step()
        time.sleep(0.05)
    assert len(ctl.pods.open_attempts()) == 4 and ctl.farms.headroom() <= 0
    run_id = start(ctl, brief(GOOD_PATCH))
    for _ in range(10):
        ctl.step()
        time.sleep(0.05)
    run = ctl.center.run(run_id)
    assert run["detail"]["phase"] == "checks" and run["detail"].get("waiting_for_capacity") is True
    assert ctl.coding.live_processes() == 0
    for t in sleepers:
        ctl.coordinator.cancel(t)
    run = drive(ctl, run_id)
    assert run["outcome"] == "accept"


def test_pause_excludes_time_from_the_wall_budget(ctl):
    """Internal review L6: a paused maintenance run does not burn its wall budget and does
    not start new check processes."""
    run_id = start(ctl, brief(GOOD_PATCH))
    pause = str(uuid.uuid4())
    ctl.center.receive_command(pause, "pause", run_id, {})
    ctl.step()
    run = ctl.center.run(run_id)
    assert run["paused"] and ctl.center.command(pause)["state"] == "applied"
    ctl.center.set_run(run_id, detail={**run["detail"], "started_at": time.time() - 590})
    for _ in range(5):
        ctl.step()
        time.sleep(0.1)
    run = ctl.center.run(run_id)
    assert run["state"] == "paused" and run["detail"].get("paused_seconds", 0) > 0
    assert ctl.coding.live_processes() == 0
    resume = str(uuid.uuid4())
    ctl.center.receive_command(resume, "resume", run_id, {})
    ctl.step()
    run = drive(ctl, run_id)
    assert run["state"] == "completed" and run["outcome"] == "accept"


EXIT_PATCH = BAD_PATCH + textwrap.dedent(
    """\
    diff --git a/tests/test_zzz_added.py b/tests/test_zzz_added.py
    new file mode 100644
    --- /dev/null
    +++ b/tests/test_zzz_added.py
    @@ -0,0 +1,7 @@
    +import os
    +
    +os._exit(0)
    +
    +
    +def test_never_runs():
    +    assert True
    """
)


def test_a_test_module_that_exits_the_runner_is_not_an_accept(ctl):
    """Internal review R1: a module that ends pytest with status 0 at import leaves no
    collected tests behind; the collected count is cross-checked against the base."""
    run_id = start(ctl, brief(EXIT_PATCH))
    run = drive(ctl, run_id)
    d = run["detail"]
    assert run["outcome"] == "reject", d.get("review")
    kinds = {f["kind"] for f in d["review"]["findings"]}
    assert "tests_not_collected" in kinds
    assert d["tests_at_base"] == 1
    good = start(ctl, brief(GOOD_PATCH))
    run = drive(ctl, good)
    assert run["outcome"] == "accept"
    assert all(c["collected"] == 2 for c in run["detail"]["checks"] if c["name"] == "pytest")
