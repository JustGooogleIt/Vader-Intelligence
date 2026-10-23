"""J3: bounded game-research farms through the controller and the real pods coordinator
(real worker subprocesses on a SYNTHETIC two-game archive; disposable root; no network,
no tmux unless available, never the operator's tmux server)."""

import json
import shutil
import time
import uuid

import pytest

from vader_intelligence.command_center import projections
from vader_intelligence.command_center.controller import Controller
from vader_intelligence.command_center.farms import build_evidence
from vader_intelligence.command_center.panes import pane_reader
from vader_intelligence.command_center.store import CenterStore
from vader_intelligence.command_center.templates import validate_request
from vader_intelligence.pods.contracts import Budgets, ExperimentManifest, PodSpec, TaskSpec
from vader_intelligence.pods.coordinator import CoordinatorConfig
from vader_intelligence.pods.game_ops import CHECK_KIND, EVIDENCE_KIND

from ._synthetic import EV1, EV2

FAST = CoordinatorConfig(
    heartbeat_seconds=0.1,
    lease_seconds=2.0,
    cancel_grace_seconds=0.3,
    backoff_base_seconds=0.05,
    backoff_max_seconds=0.2,
    poll_seconds=0.02,
    kill_wait_seconds=5.0,
)
START = {
    "template": "evidence_review_v1",
    "roles": ["evidence_inspector", "baseline_analyst", "independent_checker"],
    "limits": {"max_concurrent": 2, "max_tasks": 3, "wall_seconds": 120},
    "execution": "pods",
}


def command(center, kind, run_id=None, payload=None, command_id=None):
    command_id = command_id or str(uuid.uuid4())
    stored = center.receive_command(command_id, kind, run_id, payload or {})
    return command_id, stored


def wait(controller, predicate, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        controller.step()
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not reached")


@pytest.fixture
def controller(cfg):
    made = []

    def make(**overrides):
        c = Controller(cfg, coordinator_config=FAST, **{"tmux": False, **overrides})
        made.append(c)
        return c

    yield make
    for c in made:
        try:
            c.shutdown()
        except RuntimeError:
            pass


def run_of(cfg, run_id):
    with CenterStore(cfg.root, readonly=True) as center:
        return center.run(run_id)


def test_a_start_command_runs_a_bounded_farm_to_completion(cfg, controller):
    ctl = controller()
    cid, stored = command(
        ctl.center, "start", None, {**START, "game_id": 824948, "event_ticker": EV1}
    )
    assert stored
    report = ctl.step()
    assert [c[1] for c in report["commands"]] == ["applied"]
    run_id = report["commands"][0][2]
    receipt = ctl.center.command(cid)
    assert receipt["state"] == "applied" and [
        e["kind"] for e in ctl.center.command_events(cid)
    ] == ["accepted", "applied"]
    run = ctl.center.run(run_id)
    assert run["state"] == "running" and run["stage"] == 1 and len(run["tasks"]) == 2
    assert run["pins"]["game_id"] == 824948 and run["pins"]["event_ticker"] == EV1
    assert (
        run["pins"]["market_tickers"] == [EV1 + "-HOU"]
        and len(run["pins"]["evidence_sha256"]) == 64
    )
    assert run["pins"]["source_mode"] == "archive:fixture" and run["pins"]["code_hash"]
    wait(ctl, lambda: run_of(cfg, run_id)["state"] in ("completed", "failed"), timeout=60)
    run = run_of(cfg, run_id)
    assert run["state"] == "completed", run
    assert run["outcome"] == "consistent" and run["stage"] == 2 and len(run["tasks"]) == 3
    assert {t["role"] for t in run["tasks"]} == {
        "evidence_inspector",
        "baseline_analyst",
        "independent_checker",
    }
    view = projections.farm(cfg, run_id)
    assert view["state"] == "ok" and all(t["state"] == "succeeded" for t in view["tasks"])
    checker = next(t for t in view["tasks"] if t["role"] == "independent_checker")
    (artifact,) = [a for a in checker["result"]["artifacts"] if a["kind"] == CHECK_KIND]
    preview = projections.artifact_preview(cfg, run_id, artifact["sha256"], 65536)
    assert preview["state"] == "ok"
    report = json.loads(preview["text"])
    assert report["verdict"] == "consistent" and report["disagreements"] == []
    assert "not a forecast" in report["note"]
    analyst = next(t for t in view["tasks"] if t["role"] == "baseline_analyst")
    baseline = json.loads(
        projections.artifact_preview(
            cfg, run_id, analyst["result"]["artifacts"][0]["sha256"], 65536
        )["text"]
    )
    assert (
        baseline["baselines"][EV1 + "-HOU"]["midpoint"] == "0.6450"
    )  # (0.64 + 0.65) / 2 from the fixture book
    activity = projections.activity(cfg, run_id, 1)
    assert activity["state"] == "ok" and {e["kind"] for e in activity["events"]} >= {
        "submitted",
        "leased",
        "succeeded",
    }
    assert all(e["task_id"] in {t["task_id"] for t in run["tasks"]} for e in activity["events"])
    game = projections.game(cfg, 824948)
    assert [r["run_id"] for r in game["runs"]] == [run_id]
    assert projections.game(cfg, 824949)["runs"] == []


def test_duplicate_commands_and_a_second_start_make_one_run(cfg, controller):
    ctl = controller()
    cid = str(uuid.uuid4())
    payload = {**START, "game_id": 824948}
    assert ctl.center.receive_command(cid, "start", None, payload)
    assert ctl.center.receive_command(cid, "start", None, payload) is False  # the double click
    other, _ = command(ctl.center, "start", None, payload)
    report = ctl.step()
    states = {c[0]: c[1] for c in report["commands"]}
    assert states[cid] == "applied" and states[other] == "rejected"
    assert "run_active" in ctl.center.command(other)["reason"]
    assert len(ctl.center.runs()) == 1
    third, _ = command(ctl.center, "start", None, {**START, "game_id": 824949})
    ctl.step()
    assert (
        "one_active_farm_limit" in ctl.center.command(third)["reason"]
    )  # one active farm in the pilot
    assert len(ctl.center.runs()) == 1


def test_start_is_refused_for_an_ambiguous_or_unknown_game(cfg, controller):
    import sqlite3

    db = sqlite3.connect(cfg.archive)
    with db:
        db.execute(
            "INSERT INTO eligibility(run_id,ticker,checked_at,schedule_fetch_id,market_fetch_id,game_id,"
            "eligible,reason,cutoff,data_json) VALUES ('fixture-two-games',?,?,?,?,?,1,'eligible',?,'{}')",
            (
                EV2 + "-BOS",
                "2026-09-27T15:00:09+00:00",
                "fixture-schedule-5",
                "fixture-event-7",
                824948,
                "2026-09-27T19:03:00+00:00",
            ),
        )
    db.close()
    with pytest.raises(ValueError, match="ambiguous"):
        build_evidence(cfg, 824948)
    ctl = controller()
    a, _ = command(ctl.center, "start", None, {**START, "game_id": 824948})
    b, _ = command(ctl.center, "start", None, {**START, "game_id": 999})
    c, _ = command(ctl.center, "start", None, {**START, "game_id": 824949, "roles": ["llm_agent"]})
    d, _ = command(ctl.center, "start", None, {**START, "game_id": 824949, "execution": "shell"})
    ctl.step()
    assert "ambiguous" in ctl.center.command(a)["reason"]
    assert "unknown game" in ctl.center.command(b)["reason"]
    assert "unavailable" in ctl.center.command(c)["reason"]
    assert "not authorized" in ctl.center.command(d)["reason"]
    assert ctl.center.runs() == [] and ctl.pods.open_attempts() == []
    for bad in (
        {**START, "limits": {"max_concurrent": 9}},
        {**START, "roles": ["independent_checker"]},
        {**START, "template": "x"},
    ):
        with pytest.raises(ValueError):
            validate_request({**bad, "game_id": 1})


def test_pause_stops_new_dispatch_and_resume_continues(cfg, controller):
    ctl = controller()
    cid, _ = command(ctl.center, "start", None, {**START, "game_id": 824948})
    run_id = ctl.step()["commands"][0][2]
    pause, _ = command(ctl.center, "pause", run_id)
    ctl.step()
    assert ctl.center.command(pause)["state"] == "applied"
    wait(
        ctl,
        lambda: all(
            ctl.pods.task(t["task_id"])["state"] == "succeeded"
            for t in run_of(cfg, run_id)["tasks"]
        ),
    )
    for _ in range(5):
        ctl.step()
    run = run_of(cfg, run_id)
    assert (
        run["paused"] and run["state"] == "paused" and run["stage"] == 1 and len(run["tasks"]) == 2
    )  # no stage 2
    resume, _ = command(ctl.center, "resume", run_id)
    ctl.step()
    assert ctl.center.command(resume)["state"] == "applied"
    wait(ctl, lambda: run_of(cfg, run_id)["state"] == "completed", timeout=60)
    assert len(run_of(cfg, run_id)["tasks"]) == 3


def fill_capacity(ctl, seconds, count=4):
    """`count` long sleeps in a foreign pod: not Command Center tasks, never cancelled by it."""
    ctl.pods.register_pod(PodSpec("other", "diagnostics", Budgets(max_concurrent=count)))
    tasks = []
    for i in range(count):
        params = {"seconds": seconds + i * 0.01, "with_child": False}
        manifest = ctl.pods.put_experiment_manifest(
            ExperimentManifest(
                "0" * 64,
                {"x": "v1"},
                {"development": {"a": 1}},
                "1" * 64,
                "sleep",
                params,
                0,
                {"max_seconds": 60},
            )
        )
        tasks.append(ctl.pods.submit(TaskSpec("other", "sleep", params, manifest, 0, 1)))
    return tasks


def test_global_cap_holds_dispatch_and_cancel_drains_only_owned_work(cfg, controller):
    ctl = controller()
    sleepers = fill_capacity(ctl, seconds=20)
    wait(ctl, lambda: len(ctl.pods.open_attempts()) == 4)
    cid, _ = command(ctl.center, "start", None, {**START, "game_id": 824948})
    ctl.step()
    run_id = ctl.center.command(cid)["reason"] or next(iter(ctl.center.runs()))["run_id"]
    for _ in range(5):
        ctl.step()
    run = run_of(cfg, run_id)
    assert (
        run["state"] == "running" and run["stage"] == 0 and run["tasks"] == []
    )  # waits for capacity
    assert ctl.farms.headroom() <= 0
    cancel, _ = command(ctl.center, "cancel", run_id)
    ctl.step()
    assert ctl.center.command(cancel)["state"] == "applied"
    wait(ctl, lambda: run_of(cfg, run_id)["state"] == "canceled")
    assert len(ctl.pods.open_attempts()) == 4  # the foreign sleeps keep running
    assert all(ctl.pods.task(t)["state"] == "leased" for t in sleepers)
    for t in sleepers:
        ctl.coordinator.cancel(t)
    wait(ctl, lambda: ctl.pods.open_attempts() == [])


def test_cancel_drains_a_leased_owned_attempt_and_keeps_evidence(cfg, controller, monkeypatch):
    ctl = controller()
    cid, _ = command(ctl.center, "start", None, {**START, "game_id": 824948})
    run_id = ctl.step()["commands"][0][2]
    # Make this run's stage-1 workers slow so a cancel lands on leased attempts.
    from vader_intelligence.pods import game_ops

    monkeypatch.setattr(game_ops, "EVIDENCE_VERSION", 1)
    run = run_of(cfg, run_id)
    assert run["stage"] == 1
    cancel, _ = command(ctl.center, "cancel", run_id)
    ctl.step()
    wait(ctl, lambda: run_of(cfg, run_id)["state"] == "canceled")
    run = run_of(cfg, run_id)
    states = {ctl.pods.task(t["task_id"])["state"] for t in run["tasks"]}
    assert states <= {"canceled", "succeeded"} and run["outcome"] == "canceled"
    for t in run["tasks"]:
        for attempt in ctl.pods.attempts(t["task_id"]):
            from pathlib import Path

            assert Path(attempt["workdir"]).is_dir()  # evidence preserved


def test_controller_restart_reconciles_live_attempts_before_dispatching(cfg, controller):
    first = controller()
    sleepers = fill_capacity(first, seconds=3, count=2)
    cid, _ = command(first.center, "start", None, {**START, "game_id": 824949})
    first.step()  # applies the command, submits stage 1 (2 tasks queued)
    first.step()  # the coordinator spawns workers for the queued tasks
    run_id = first.center.command(cid)["reason"] or next(iter(first.center.runs()))["run_id"]
    tasks_before = {t["task_id"] for t in run_of(cfg, run_id)["tasks"]}
    assert len(tasks_before) == 2
    killed = first.shutdown()["killed"]  # a crash: live attempts recorded lost, workers gone
    assert killed
    second = controller()
    assert second.pods.open_attempts() == []
    wait(second, lambda: run_of(cfg, run_id)["state"] == "completed", timeout=60)
    run = run_of(cfg, run_id)
    assert {t["task_id"] for t in run["tasks"]} >= tasks_before  # same content ids, no duplicates
    assert len(run["tasks"]) == 3
    for task_id in tasks_before:
        kinds = [e["kind"] for e in second.pods.events(task_id)]
        assert kinds.count("submitted") == 1
    for t in sleepers:
        assert second.pods.task(t)["state"] in (
            "succeeded",
            "failed",
            "lost",
            "queued",
            "retry_wait",
            "leased",
            "canceled",
        )


def test_two_runs_for_two_games_do_not_share_tasks_artifacts_or_panes(cfg, controller):
    ctl = controller()
    a_cmd, _ = command(ctl.center, "start", None, {**START, "game_id": 824948})
    run_a = ctl.step()["commands"][0][2]
    wait(ctl, lambda: run_of(cfg, run_a)["state"] == "completed", timeout=60)
    b_cmd, _ = command(ctl.center, "start", None, {**START, "game_id": 824949})
    run_b = ctl.step()["commands"][0][2]
    wait(ctl, lambda: run_of(cfg, run_b)["state"] == "completed", timeout=60)
    fa, fb = projections.farm(cfg, run_a), projections.farm(cfg, run_b)
    ta, tb = {t["task_id"] for t in fa["tasks"]}, {t["task_id"] for t in fb["tasks"]}
    assert ta and tb and not ta & tb
    assert fa["run"]["pins"]["evidence_sha256"] != fb["run"]["pins"]["evidence_sha256"]
    sha_b = fb["tasks"][0]["result"]["artifacts"][0]["sha256"]
    assert (
        projections.artifact_preview(cfg, run_a, sha_b, 1024)["state"] == "empty"
    )  # not declared by run A
    assert projections.artifact_preview(cfg, run_b, sha_b, 1024)["state"] == "ok"
    assert projections.game(cfg, 824948)["runs"][0]["run_id"] == run_a
    assert projections.game(cfg, 824949)["runs"][0]["run_id"] == run_b
    stored = {
        row[0]: row[1]
        for row in ctl.pods.db.execute(
            "SELECT sha256, kind FROM artifacts WHERE kind=?", (EVIDENCE_KIND,)
        )
    }
    assert len(stored) == 2


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_live_attempts_get_registered_panes_on_a_disposable_server(cfg, controller):
    ctl = controller(tmux=True)
    assert ctl.tmux is not None and ctl.tmux.alive()
    assert ctl.tmux.socket.name.endswith(".sock") and "cc-" in ctl.tmux.socket.name
    sleepers = fill_capacity(ctl, seconds=5, count=1)  # foreign: no pane
    cid, _ = command(
        ctl.center,
        "start",
        None,
        {
            **START,
            "game_id": 824948,
            "limits": {"max_concurrent": 1, "max_tasks": 3, "wall_seconds": 120},
        },
    )
    ctl.step()
    wait(ctl, lambda: bool(ctl.registered), timeout=20)
    run_id = next(iter(ctl.center.runs()))["run_id"]
    registration = next(iter(ctl.registered.values()))
    assert registration["run_id"] == run_id and registration["pane_id"].startswith("%")
    assert ctl.tmux.pane_alive(registration["pane_id"])
    config = json.loads((cfg.root / "tmux" / "observer.json").read_text())
    assert config["workspace"] == str(cfg.root) and [w["worker_id"] for w in config["workers"]] == [
        registration["worker_id"]
    ]
    assert all(
        w["task_id"] in {t["task_id"] for t in run_of(cfg, run_id)["tasks"]}
        for w in config["workers"]
    )
    out = ctl.tmux._run(
        "show-options", "-p", "-t", registration["pane_id"], "-v", "@vader_run"
    ).strip()
    assert out == run_id
    worker = registration["worker_id"].split(":", 1)[1]
    with CenterStore(cfg.root, readonly=True) as center:
        (row,) = center.workers(run_id)
        assert row["worker_id"] == worker and row["pane_id"] == registration["pane_id"]

    # Pane capture goes only through the observer; a stubbed observer answer is data.
    class Done:
        def __init__(self, code, out):
            self.returncode, self.stdout, self.stderr = code, out, ""

    def matched(argv, **kw):
        assert argv[1:4] == ["-m", "tools.worker_observer", "--config"] and "inspect" in argv
        return Done(
            0,
            json.dumps(
                {
                    "identity": "matched",
                    "observed_at": "2026-09-27T18:00:00+00:00",
                    "terminal": "<script>alert(1)</script>\n\x1b[31mred\x1b[0m\nDONE: task complete",
                }
            ),
        )

    view = pane_reader(cfg, run_id, worker, 60, runner=matched)
    assert (
        view["state"] == "ok"
        and view["identity"] == "matched"
        and view["completion_verified"] is False
    )
    assert (
        "<script>" in view["text"]
    )  # returned verbatim as a JSON string; the page renders it inert

    def mismatch(argv, **kw):
        return Done(
            1,
            json.dumps(
                {"identity": "unknown", "reason": "identity_mismatch", "terminal": "LEAKED"}
            ),
        )

    view = pane_reader(cfg, run_id, worker, 60, runner=mismatch)
    assert view["state"] == "unavailable" and view["identity"] == "unknown" and view["text"] is None
    assert pane_reader(cfg, run_id, "nobody", 60, runner=matched)["state"] == "empty"
    for t in sleepers:
        ctl.coordinator.cancel(t)
    wait(ctl, lambda: run_of(cfg, run_id)["state"] == "completed", timeout=60)
    socket_path = ctl.tmux.socket
    ctl.shutdown()
    assert not socket_path.exists()
