"""Pod autopilot (roadmap G): plan, submit and run approved operations in bounded rounds.

Each round drains the pod's unfinished work, builds the development-only context
(`pods.planner.context`), asks the planner for proposals and validates them against the
operations' typed contracts. It then stores the round as a content-addressed
`planner_round` artifact (context hash, accepted task ids, rejections by hash and reason,
provider request/response hashes) chained to the pod's previous round on the snapshot,
and only then submits the planned tasks and runs the coordinator until idle. So no task
exists that no round names. The loop ends when nothing new is proposed, or at the round,
task or wall-time bound; `max_tasks` is the pod's total task budget on the snapshot,
counted across runs.

A crash is recovered by running the loop again: unfinished tasks are drained first, and
work planned but not submitted is proposed again (identical for a deterministic planner;
task ids are content hashes, so nothing runs twice).

With `accept_best`, the pod's best finished model on the snapshot by its development
score (compared only among tasks with the same fold setting) is accepted as one candidate
and judged by the protected evaluator; the holdout is never touched here.
"""

import json
import time

from ..provenance import json_text, source_hash
from . import evaluator
from .candidates import candidate_record
from .contracts import ContractError, ExperimentManifest, TaskSpec, content_hash, task_identity
from .operations import normalized_params
from .planner import PLANNED_OPERATIONS, SCORE_FIELDS, context, rejection, score, validate
from .snapshot import feature_set_version, load

ROUND_KIND = "planner_round"
UNFINISHED = ("queued", "leased", "retry_wait")
MAX_ROUNDS = 50
MAX_TASKS = 200


def prepare_operation(store, pod_id, operation, params, *, max_attempts=3, priority=0, budget=None):
    """(TaskSpec, task id) for `params`, with its experiment manifest stored; not submitted."""
    params = normalized_params(operation, params)
    snapshot = load(store.artifacts.get_bytes(params["dataset_manifest_hash"]))
    manifest = ExperimentManifest(
        dataset_manifest_hash=params["dataset_manifest_hash"],
        feature_set_versions={snapshot.feature_set: feature_set_version(snapshot.manifest)},
        split={
            "development": {"through": snapshot.split.development_through},
            "validation": {"through": snapshot.split.validation_through},
        },
        code_hash=source_hash(),
        operation=operation,
        params=params,
        seed=int(params.get("seed", 0)),
        budget=budget or {"pod": pod_id},
    )
    manifest_sha = store.put_experiment_manifest(manifest)
    task = TaskSpec(pod_id, operation, params, manifest_sha, priority, max_attempts)
    return task, task_identity(operation, params, manifest_sha)


def submit_operation(store, pod_id, operation, params, **options):
    """Store the experiment manifest for `params` and submit the task; its id."""
    task, _ = prepare_operation(store, pod_id, operation, params, **options)
    return store.submit(task)


def _fold_key(entry):
    """Scores are comparable only on the same folds (train_model) or search kind."""
    if entry["operation"] == "train_model":
        return ("train_model", entry["params"]["hyperparameters"].get("n_folds", 4))
    return (entry["operation"],)


def best_task(store, pod_id, snapshot_sha):
    """(task_id, score, compared) of the pod's best finished model on the snapshot among
    tasks comparable with its most recent scored task, or None."""
    ctx = context(store, pod_id, snapshot_sha, tasks_remaining=0, rounds_remaining=0)
    scored = [
        (h, score(h["result"], h["operation"]))
        for h in ctx["history"]
        if h["state"] in ("succeeded", "partial") and "result" in h
    ]
    scored = [(h, s) for h, s in scored if s is not None]
    if not scored:
        return None
    key = _fold_key(scored[-1][0])
    group = [(s, h["task_id"]) for h, s in scored if _fold_key(h) == key]
    best_score, task_id = min(group)
    return task_id, best_score, len(group)


def _last_round(store, pod_id, snapshot_sha):
    """(sha256, round) of the pod's latest planner round on the snapshot, or (None, 0)."""
    last = (None, 0)
    rows = store.db.execute(
        "SELECT sha256 FROM artifacts WHERE kind=? ORDER BY created_at, sha256", (ROUND_KIND,)
    )
    for (sha,) in rows:
        record = json.loads(store.artifacts.get_bytes(sha))
        if record["pod"] == pod_id and record["snapshot_sha256"] == snapshot_sha:
            if record["round"] > last[1]:
                last = (sha, record["round"])
    return last


def _tasks_on_snapshot(store, pod_id, snapshot_sha, states=None):
    rows = store.db.execute("SELECT params_json, state FROM tasks WHERE pod_id=?", (pod_id,))
    return sum(
        1
        for params_json, state in rows
        if json.loads(params_json).get("dataset_manifest_hash") == snapshot_sha
        and (states is None or state in states)
    )


def run(
    store,
    registry,
    *,
    pod_id,
    snapshot_sha,
    planner,
    coordinator,
    rounds=3,
    max_tasks=12,
    max_tasks_per_round=4,
    deadline_seconds=3600.0,
    max_steps_per_round=1_000_000,
    accept_best=False,
    criteria=None,
    clock=time.monotonic,
):
    """Run the bounded planning loop for one pod; returns a JSON-ready summary."""
    if not (1 <= rounds <= MAX_ROUNDS and 1 <= max_tasks <= MAX_TASKS):
        raise ContractError(f"rounds must be 1..{MAX_ROUNDS} and max_tasks 1..{MAX_TASKS}")
    if max_tasks_per_round < 1 or deadline_seconds <= 0:
        raise ContractError("max_tasks_per_round and deadline_seconds must be positive")
    end = clock() + deadline_seconds
    submitted, records, stop = [], [], "rounds_exhausted"
    previous, number = _last_round(store, pod_id, snapshot_sha)
    for index in range(rounds):
        remaining = max_tasks - _tasks_on_snapshot(store, pod_id, snapshot_sha)
        if remaining <= 0:
            stop = "task_budget_exhausted"
            break
        if clock() >= end:
            stop = "deadline"
            break
        # Finish this pod's queued work first (a resumed loop after a crash). The
        # coordinator runs every pod's queue; tasks blocked by a pod budget stay queued.
        if _tasks_on_snapshot(store, pod_id, snapshot_sha, UNFINISHED):
            report = coordinator.run(max_steps_per_round, max(end - clock(), 0.001))
            if report.get("stopped") != "idle":
                stop = f"coordinator_{report.get('stopped')}"
                break
            if _tasks_on_snapshot(store, pod_id, snapshot_sha, UNFINISHED):
                stop = "tasks_blocked"
                break
        ctx = context(
            store,
            pod_id,
            snapshot_sha,
            tasks_remaining=remaining,
            rounds_remaining=rounds - index,
        )
        plan = planner.plan(ctx)
        accepted, rejected, new_ids, tasks = [], list(plan.rejected), [], []
        known = {h["task_id"] for h in ctx["history"]}
        for proposal in plan.proposals:
            item = {"operation": proposal.operation, "params": proposal.params}
            try:
                params = validate(ctx, proposal)
            except ContractError as exc:
                rejected.append(rejection(item, str(exc)))
                continue
            task, task_id = prepare_operation(store, pod_id, proposal.operation, params)
            if task_id in known or task_id in new_ids:
                accepted.append({"operation": proposal.operation, "task_id": task_id})
                continue
            # Bounds are checked before submission: a submitted task will run.
            if len(new_ids) >= min(max_tasks_per_round, remaining):
                rejected.append(rejection(item, "task limit"))
                continue
            accepted.append({"operation": proposal.operation, "task_id": task_id})
            new_ids.append(task_id)
            tasks.append((task, task_id))
        number += 1
        record = {
            "kind": ROUND_KIND,
            "round": number,
            "previous": previous,
            "pod": pod_id,
            "snapshot_sha256": snapshot_sha,
            "planner": planner.name,
            "context_hash": content_hash(ctx),
            "provider": plan.provider,
            "accepted": accepted,
            "rejected": rejected,
            "new_task_ids": new_ids,
            "planner_stop": plan.stop,
        }
        previous = store.put_input(json_text(record).encode(), ROUND_KIND)
        records.append({"sha256": previous} | _brief(record))
        for task, task_id in tasks:  # only after the round naming them is stored
            if store.submit(task) != task_id:
                raise RuntimeError("submitted task id differs from the planned id")
            submitted.append(task_id)
        if not new_ids:
            stop = plan.stop or "no_new_tasks"
            break
        report = coordinator.run(max_steps_per_round, max(end - clock(), 0.001))
        records[-1]["coordinator"] = report
        if report.get("stopped") != "idle":
            stop = f"coordinator_{report.get('stopped')}"
            break
    summary = {
        "pod": pod_id,
        "snapshot_sha256": snapshot_sha,
        "planner": planner.name,
        "rounds": records,
        "submitted": submitted,
        "stop": stop,
        "score_field": SCORE_FIELDS.get(_operation(store, pod_id) or ""),
        "evidence": "planning record; development scores only; not promotion evidence",
    }
    best = best_task(store, pod_id, snapshot_sha)
    summary["best"] = (
        None
        if best is None
        else {"task_id": best[0], "development_score": best[1], "compared_tasks": best[2]}
    )
    if accept_best and best is not None:
        candidate_id = evaluator.accept_model(store, registry, best[0])
        record = candidate_record(store, candidate_id)
        if record["state"] == "proposed":
            summary["candidate"] = evaluator.evaluate(store, registry, candidate_id, criteria)
        else:  # a resumed loop: the candidate was already decided
            summary["candidate"] = {"candidate_id": candidate_id, "state": record["state"]}
    return summary


def _operation(store, pod_id):
    row = store.db.execute("SELECT role FROM pods WHERE pod_id=?", (pod_id,)).fetchone()
    return (PLANNED_OPERATIONS.get(row[0]) or (None,))[0] if row else None


def _brief(record):
    return {
        "round": record["round"],
        "accepted": record["accepted"],
        "rejected": [r["reason"] for r in record["rejected"]],
        "new_task_ids": record["new_task_ids"],
        "planner_stop": record["planner_stop"],
    }
