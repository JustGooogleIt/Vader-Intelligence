"""Pod autopilot on a SYNTHETIC registry dataset (roadmap G): bounded planning rounds of
approved operations, development-only planning context, provider proposals validated
like submissions, crash resume and the protected evaluator as the only judge."""

import json

import pytest
from datasets.test_training import SPLIT

from vader_intelligence.cli import execute, parser
from vader_intelligence.pods import autopilot, evaluator, planner
from vader_intelligence.pods.candidates import candidate_record
from vader_intelligence.pods.contracts import ContractError
from vader_intelligence.pods.providers import ScriptedProvider, UnwiredVendorProvider
from vader_intelligence.pods.snapshot import feature_set_version

from .test_research_pods import research  # noqa: F401  (fixture)

SMALL = {"logistic": {"C": [0.1, 1.0]}}


def heuristic(**overrides):
    options = {"families": ("logistic",), "grids": SMALL, "n_folds": 3} | overrides
    return planner.HeuristicPlanner(**options)


def setup(pods, registry, dataset_id, role="baseline_models"):
    snapshot_sha = evaluator.snapshot_dataset(pods.store, registry, dataset_id, SPLIT)
    pod = pods.pod("auto", role)
    return snapshot_sha, pod


def rounds(pods, summary):
    return [json.loads(pods.store.artifacts.get_bytes(r["sha256"])) for r in summary["rounds"]]


def test_heuristic_autopilot_refines_on_development_scores_then_stops(research, pods):  # noqa: F811
    registry, dataset_id, manifest, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    summary = autopilot.run(
        pods.store,
        registry,
        pod_id=pod,
        snapshot_sha=snapshot_sha,
        planner=heuristic(),
        coordinator=pods.coordinator(),
        rounds=5,
        max_tasks=5,
        deadline_seconds=240,
    )
    records = rounds(pods, summary)
    assert records[0]["accepted"][0]["operation"] == "train_model"
    assert records[0]["previous"] is None
    assert all(r["previous"] == prev["sha256"] for r, prev in zip(records[1:], summary["rounds"]))
    assert {pods.state(t) for t in summary["submitted"]} == {"succeeded"}
    assert 2 <= len(summary["submitted"]) <= 5  # refined at least once, within the budget
    assert summary["stop"] in ("no_improving_refinement", "task_budget_exhausted")
    # Refinement grids follow the development-selected C (an edge extends the grid).
    first = pods.store.task(summary["submitted"][0])
    second = json.loads(pods.store.task(summary["submitted"][1])["params_json"])
    assert json.loads(first["params_json"])["hyperparameters"]["grid"] == SMALL["logistic"]
    assert second["hyperparameters"]["grid"]["C"] != SMALL["logistic"]["C"]
    assert summary["best"]["development_score"] > 0
    assert summary["score_field"] == "development_cv_log_loss"
    kinds = pods.store.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind=?", (autopilot.ROUND_KIND,)
    ).fetchone()[0]
    assert kinds == len(records)


def test_planning_context_is_development_only(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    autopilot.run(
        pods.store,
        registry,
        pod_id=pod,
        snapshot_sha=snapshot_sha,
        planner=heuristic(),
        coordinator=pods.coordinator(),
        rounds=1,
        deadline_seconds=120,
    )
    ctx = planner.context(pods.store, pod, snapshot_sha, tasks_remaining=1, rounds_remaining=1)
    (task,) = ctx["history"]
    output = pods.store.result(task["task_id"])["result"]["output"]
    assert "diagnostic_validation_log_loss" in output  # the pod computed it ...
    text = json.dumps(ctx)
    assert "validation" not in text  # ... but no validation field or value reaches planning
    assert repr(output["diagnostic_validation_log_loss"]) not in text
    assert "lease_token" not in text and "holdout" not in text
    assert task["result"]["development_cv_log_loss"] == output["development_cv_log_loss"]


def test_provider_proposals_are_validated_like_submissions(research, pods):  # noqa: F811
    registry, dataset_id, manifest, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    ctx = planner.context(pods.store, pod, snapshot_sha, tasks_remaining=12, rounds_remaining=1)
    good = {"model_family": "logistic", "seed": 0, "hyperparameters": {"grid": {"C": [1.0]}}}
    response = {
        "proposals": [
            {"operation": "train_model", "params": good, "rationale": "small logistic"},
            {"operation": "shell", "params": {"cmd": "rm -rf /"}},
            {"operation": "train_model", "params": good, "code": "import os"},
            {
                "operation": "train_model",
                "params": good | {"hyperparameters": {"grid": {"__import__": [1]}}},
            },
            {"operation": "train_model", "params": good | {"dataset_manifest_hash": "0" * 64}},
            {"operation": "robustness_analysis", "params": {}},
            {"operation": "train_model", "params": good | {"split": "validation"}},
        ]
    }
    provider = ScriptedProvider({planner.ProviderPlanner(None).prompt(ctx): json.dumps(response)})
    summary = autopilot.run(
        pods.store,
        registry,
        pod_id=pod,
        snapshot_sha=snapshot_sha,
        planner=planner.ProviderPlanner(provider),
        coordinator=pods.coordinator(),
        rounds=1,
        deadline_seconds=120,
    )
    (record,) = rounds(pods, summary)
    assert len(provider.calls) == 1 and len(summary["submitted"]) == 1
    reasons = [r["reason"] for r in record["rejected"]]
    assert len(reasons) == 6
    assert any("not planned" in r for r in reasons)  # unknown and unplanned operations
    assert "proposal shape" in reasons
    assert any("grid keys" in r for r in reasons)
    assert any("fixed by the snapshot" in r for r in reasons)
    assert any("development rows only" in r for r in reasons)
    assert set(record["provider"]) == {"name", "request_hash", "response_hash", "cost_usd"}
    params = json.loads(pods.store.task(summary["submitted"][0])["params_json"])
    assert params["feature_set_version"] == feature_set_version(manifest)
    assert pods.state(summary["submitted"][0]) == "succeeded"


def test_a_real_vendor_planner_is_refused_and_submits_nothing(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    summary = autopilot.run(
        pods.store,
        registry,
        pod_id=pod,
        snapshot_sha=snapshot_sha,
        planner=planner.ProviderPlanner(UnwiredVendorProvider("any-vendor")),
        coordinator=pods.coordinator(),
        rounds=3,
    )
    assert summary["stop"] == "paid_calls_disabled" and summary["submitted"] == []
    (record,) = rounds(pods, summary)
    assert "paid model calls are disabled" in record["provider"]["error"]
    assert pods.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


class Crash(BaseException):
    pass


class CrashingCoordinator:
    def run(self, *args, **kwargs):
        raise Crash


def test_a_crashed_loop_resumes_without_duplicate_work(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    options = dict(pod_id=pod, snapshot_sha=snapshot_sha, rounds=3, max_tasks=3)
    with pytest.raises(Crash):  # submitted, then the process died before running it
        autopilot.run(
            pods.store, registry, planner=heuristic(), coordinator=CrashingCoordinator(), **options
        )
    queued = [r[0] for r in pods.store.db.execute("SELECT task_id FROM tasks")]
    assert len(queued) == 1
    summary = autopilot.run(
        pods.store,
        registry,
        planner=heuristic(),
        coordinator=pods.coordinator(),
        deadline_seconds=240,
        **options,
    )
    assert pods.state(queued[0]) == "succeeded"  # drained first, not resubmitted
    assert queued[0] not in summary["submitted"]
    ids = [r[0] for r in pods.store.db.execute("SELECT task_id FROM tasks")]
    assert len(ids) == len(set(ids)) == 1 + len(summary["submitted"])


def test_bounds_are_checked_before_submission(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    two = heuristic(families=("logistic", "gradient_boosting"))
    options = dict(pod_id=pod, snapshot_sha=snapshot_sha, planner=two)
    with pytest.raises(Crash):  # only submissions happen before the coordinator runs
        autopilot.run(
            pods.store,
            registry,
            coordinator=CrashingCoordinator(),
            rounds=1,
            max_tasks_per_round=1,
            **options,
        )
    assert pods.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    (sha,) = [
        r[0]
        for r in pods.store.db.execute(
            "SELECT sha256 FROM artifacts WHERE kind=?", (autopilot.ROUND_KIND,)
        )
    ]
    record = json.loads(pods.store.artifacts.get_bytes(sha))
    assert [r["reason"] for r in record["rejected"]] == ["task limit"]
    with pytest.raises(ContractError):
        autopilot.run(pods.store, registry, coordinator=CrashingCoordinator(), rounds=0, **options)


def test_accept_best_is_judged_by_the_protected_evaluator_once(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    options = dict(
        pod_id=pod,
        snapshot_sha=snapshot_sha,
        planner=heuristic(),
        rounds=2,
        max_tasks=2,
        deadline_seconds=240,
        accept_best=True,
        criteria=evaluator.Criteria(min_n=10, allow_weaker=True),
    )
    coordinator = pods.coordinator()
    summary = autopilot.run(pods.store, registry, coordinator=coordinator, **options)
    decision = summary["candidate"]
    assert decision["state"] in ("validated", "rejected")
    assert decision["recomputed"]["status"] == "computed"
    record = candidate_record(pods.store, decision["candidate_id"])
    assert record["spec"]["model_sha256"]
    assert record["source_task_id"] == summary["best"]["task_id"]
    again = autopilot.run(pods.store, registry, coordinator=coordinator, **options)
    assert again["candidate"] == {
        "candidate_id": decision["candidate_id"],
        "state": record["state"],
    }


def test_cli_autopilot_with_a_vendor_planner_makes_no_call(research, pods, tmp_path):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    result = execute(
        parser().parse_args(
            ["pods", "autopilot", "--root", str(pods.root), "--pod", pod]
            + ["--snapshot", snapshot_sha, "--planner", "vendor", "--vendor", "any-vendor"]
        )
    )
    assert result["status"] == "complete" and result["stop"] == "paid_calls_disabled"
    assert result["submitted"] == []
    with pytest.raises(ValueError, match="registry"):
        execute(
            parser().parse_args(
                ["pods", "autopilot", "--root", str(pods.root), "--pod", pod]
                + ["--snapshot", snapshot_sha, "--accept-best"]
            )
        )


def test_heuristic_refinement_brackets_over_every_tried_value():
    refine = planner.HeuristicPlanner._refine
    tried = {"C": {0.00033, 0.001, 0.0033, 0.01, 0.1, 1.0}}
    best = {"selected_config": {"C": 0.0033}}
    (low, mid, high) = refine("logistic", best, tried)["C"]
    assert mid == 0.0033 and 0.001 < low < mid < high < 0.01  # between tried neighbours
    assert refine("logistic", {"selected_config": {"C": 0.00033}}, tried)["C"] == [
        1.1e-05,
        3.3e-05,
        0.00011,
    ]
    assert refine("logistic", {"selected_config": {"C": 1.0}}, tried)["C"] == [3.0, 10.0, 30.0]
    gbm = {
        "selected_config": {
            "learning_rate": 0.05,
            "max_iter": 100,
            "max_leaf_nodes": 7,
            "min_samples_leaf": 40,
            "l2_regularization": 1.0,
        }
    }
    grid = refine("gradient_boosting", gbm, {})
    assert grid["learning_rate"] == [0.025, 0.05] and grid["max_leaf_nodes"] == [3, 7, 14]
    ctx = {"role": "diagnostics", "history": []}
    assert planner.HeuristicPlanner().plan(ctx).stop == "role diagnostics is not planned"


class Counting:
    """Test planner: one new logistic task per round (a distinct C each time)."""

    name = "counting-test"

    def plan(self, ctx):
        c = 0.1 * (len(ctx["history"]) + 1)
        params = {"model_family": "logistic", "seed": 0}
        params["hyperparameters"] = {"grid": {"C": [round(c, 6)]}, "n_folds": 3}
        return planner.Plan((planner.Proposal("train_model", params),))


class IdleCoordinator:
    def run(self, *args, **kwargs):
        return {"stopped": "idle", "steps": 0}


def test_malformed_provider_output_is_rejected_and_recorded(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    ctx = planner.context(pods.store, pod, snapshot_sha, tasks_remaining=12, rounds_remaining=1)
    good = {"model_family": "logistic", "seed": 0, "hyperparameters": {"grid": {"C": [1.0]}}}
    bad = [
        good | {"hyperparameters": [1]},
        good | {"hyperparameters": "x"},
        good | {"hyperparameters": {"grid": ["C"]}},
        good | {"hyperparameters": {"grid": {"C": 5}}},
        good | {"hyperparameters": {"grid": {"C": [True]}}},
        good | {"model_family": {"nested": 1}},
        good | {"seed": "zero"},
    ]
    response = {
        "proposals": [{"operation": "train_model", "params": good}]
        + [{"operation": "train_model", "params": params} for params in bad]
    }
    prompt = planner.ProviderPlanner(None).prompt(ctx)
    provider = ScriptedProvider({prompt: json.dumps(response)})
    summary = autopilot.run(
        pods.store,
        registry,
        pod_id=pod,
        snapshot_sha=snapshot_sha,
        planner=planner.ProviderPlanner(provider),
        coordinator=IdleCoordinator(),
        rounds=1,
    )
    (record,) = rounds(pods, summary)
    assert len(summary["submitted"]) == 1 and len(record["rejected"]) == len(bad)
    for entry in record["rejected"]:  # hashes and reasons, never the provider's values
        assert set(entry) == {"proposal_sha256", "operation", "reason"}
    assert "nested" not in json.dumps(record)


def test_non_finite_json_and_provider_errors_end_planning(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    ctx = planner.context(pods.store, pod, snapshot_sha, tasks_remaining=12, rounds_remaining=1)
    prompt = planner.ProviderPlanner(None).prompt(ctx)
    text = '{"proposals": [{"operation": "train_model", "params": {"seed": NaN}}]}'
    options = dict(pod_id=pod, snapshot_sha=snapshot_sha, coordinator=IdleCoordinator(), rounds=1)
    nan = autopilot.run(
        pods.store,
        registry,
        planner=planner.ProviderPlanner(ScriptedProvider({prompt: text})),
        **options,
    )
    assert nan["stop"] == "response_not_json" and nan["submitted"] == []
    unscripted = autopilot.run(
        pods.store, registry, planner=planner.ProviderPlanner(ScriptedProvider({})), **options
    )
    assert unscripted["stop"] == "provider_error"
    (record,) = rounds(pods, unscripted)
    assert record["provider"]["error"] == "UnscriptedRequest"


def test_a_round_is_recorded_before_its_tasks_are_submitted(research, pods, monkeypatch):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    options = dict(pod_id=pod, snapshot_sha=snapshot_sha, planner=Counting(), rounds=1)

    def crash(task, now=None):
        raise Crash

    monkeypatch.setattr(pods.store, "submit", crash)
    with pytest.raises(Crash):
        autopilot.run(pods.store, registry, coordinator=IdleCoordinator(), **options)
    monkeypatch.undo()
    (sha,) = [
        r[0]
        for r in pods.store.db.execute(
            "SELECT sha256 FROM artifacts WHERE kind=?", (autopilot.ROUND_KIND,)
        )
    ]
    first = json.loads(pods.store.artifacts.get_bytes(sha))
    assert pods.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    resumed = autopilot.run(pods.store, registry, coordinator=IdleCoordinator(), **options)
    assert resumed["submitted"] == first["new_task_ids"]  # the planned task, now submitted
    (second,) = rounds(pods, resumed)
    assert second["previous"] == sha and second["round"] == 2


def test_the_task_budget_and_round_chain_span_runs(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    coordinator = pods.coordinator()
    options = dict(pod_id=pod, snapshot_sha=snapshot_sha, planner=Counting(), deadline_seconds=240)
    first = autopilot.run(
        pods.store, registry, coordinator=coordinator, rounds=5, max_tasks=2, **options
    )
    assert len(first["submitted"]) == 2 and first["stop"] == "task_budget_exhausted"
    again = autopilot.run(
        pods.store, registry, coordinator=coordinator, rounds=5, max_tasks=2, **options
    )
    assert again["submitted"] == [] and again["stop"] == "task_budget_exhausted"
    more = autopilot.run(
        pods.store, registry, coordinator=coordinator, rounds=1, max_tasks=3, **options
    )
    (record,) = rounds(pods, more)
    assert len(more["submitted"]) == 1 and record["round"] == 3
    assert record["previous"] == first["rounds"][-1]["sha256"]
    assert more["best"]["compared_tasks"] == 3


def test_tasks_blocked_in_the_queue_stop_the_loop(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id)
    options = dict(pod_id=pod, snapshot_sha=snapshot_sha, planner=Counting(), rounds=3)
    with pytest.raises(Crash):
        autopilot.run(pods.store, registry, coordinator=CrashingCoordinator(), **options)
    blocked = autopilot.run(pods.store, registry, coordinator=IdleCoordinator(), **options)
    assert blocked["stop"] == "tasks_blocked" and blocked["submitted"] == []


def test_symbolic_planning_context_is_development_only(research, pods):  # noqa: F811
    registry, dataset_id, _, _ = research
    snapshot_sha, pod = setup(pods, registry, dataset_id, role="symbolic_search")
    small = {"population": 50, "max_generations": 4, "max_candidates": 1000, "max_seconds": 60.0}
    summary = autopilot.run(
        pods.store,
        registry,
        pod_id=pod,
        snapshot_sha=snapshot_sha,
        planner=planner.HeuristicPlanner(symbolic=small),
        coordinator=pods.coordinator(),
        rounds=1,
        deadline_seconds=120,
    )
    assert summary["score_field"] == "development_inner_log_loss"
    ctx = planner.context(pods.store, pod, snapshot_sha, tasks_remaining=1, rounds_remaining=1)
    (task,) = ctx["history"]
    output = pods.store.result(task["task_id"])["result"]["output"]
    assert "diagnostic_validation_log_loss" in output
    text = json.dumps(ctx)
    assert "validation" not in text and repr(output["diagnostic_validation_log_loss"]) not in text
    assert task["result"]["development_inner_log_loss"] == output["development_inner_log_loss"]
