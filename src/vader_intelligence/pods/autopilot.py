"""Pod autopilot (roadmap G): plan, submit and run approved operations in bounded rounds.

Each round builds the development-only context (`pods.planner.context`), asks the
planner for proposals, validates them against the operations' typed contracts, submits
the new ones and runs the coordinator until the queue is idle. The loop ends when the
planner proposes nothing new, or at the round, task or wall-time bound.

Every round is stored as a content-addressed `planner_round` artifact (the context hash,
the planner's proposals and rejections, provider request/response hashes, the task ids)
chained to the previous round. Tasks are idempotent by content, so a crashed loop is
resumed by running it again: a deterministic planner re-proposes the same work and the
store returns the existing task ids.

With `accept_best`, the pod's best finished model on the snapshot by its development
score is accepted as one candidate and judged by the protected evaluator; the holdout is
never touched here.
"""

import json
import time

from ..provenance import json_text, source_hash
from . import evaluator
from .candidates import candidate_record
from .contracts import ContractError, ExperimentManifest, TaskSpec, content_hash
from .planner import PLANNED_OPERATIONS, SCORE_FIELDS, Proposal, context, score, validate
from .snapshot import feature_set_version, load

ROUND_KIND = "planner_round"
MAX_ROUNDS = 50
MAX_TASKS = 200


def submit_operation(store, pod_id, operation, params, *, max_attempts=3, priority=0, budget=None):
    """Store the experiment manifest for `params` and submit the task; its id."""
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
    return store.submit(task)


def best_task(store, pod_id, snapshot_sha):
    """(task_id, score) of the pod's best finished model on the snapshot, or None."""
    ctx = context(store, pod_id, snapshot_sha, tasks_remaining=0, rounds_remaining=0)
    scored = [
        (score(h["result"], h["operation"]), h["task_id"])
        for h in ctx["history"]
        if h["state"] in ("succeeded", "partial") and "result" in h
    ]
    scored = [(s, t) for s, t in scored if s is not None]
    if not scored:
        return None
    best_score, task_id = min(scored)
    return task_id, best_score


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
    submitted, records, previous, stop = [], [], None, "rounds_exhausted"
    for index in range(rounds):
        remaining = max_tasks - len(submitted)
        if remaining <= 0:
            stop = "task_budget_exhausted"
            break
        if clock() >= end:
            stop = "deadline"
            break
        # Finish this pod's queued work first (a resumed loop after a crash).
        if _unfinished(store, pod_id, snapshot_sha):
            report = coordinator.run(max_steps_per_round, max(end - clock(), 0.001))
            if report.get("stopped") != "idle":
                stop = f"coordinator_{report.get('stopped')}"
                break
        ctx = context(
            store,
            pod_id,
            snapshot_sha,
            tasks_remaining=remaining,
            rounds_remaining=rounds - index,
        )
        plan = planner.plan(ctx)
        accepted, rejected, new_ids = [], list(plan.rejected), []
        known = {h["task_id"] for h in ctx["history"]}
        for position, proposal in enumerate(plan.proposals):
            # Bounds are checked before submission: a submitted task will run.
            if position >= max_tasks_per_round or len(new_ids) >= remaining:
                rejected.append({"proposal": _plain(proposal), "reason": "task limit"})
                continue
            try:
                params = validate(ctx, proposal)
            except ContractError as exc:
                rejected.append({"proposal": _plain(proposal), "reason": str(exc)})
                continue
            task_id = submit_operation(store, pod_id, proposal.operation, params)
            accepted.append({"operation": proposal.operation, "task_id": task_id})
            if task_id not in known and task_id not in new_ids:
                new_ids.append(task_id)
        record = {
            "kind": ROUND_KIND,
            "round": index + 1,
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
        submitted.extend(new_ids)
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
    summary["best"] = None if best is None else {"task_id": best[0], "development_score": best[1]}
    if accept_best and best is not None:
        candidate_id = evaluator.accept_model(store, registry, best[0])
        record = candidate_record(store, candidate_id)
        if record["state"] == "proposed":
            summary["candidate"] = evaluator.evaluate(store, registry, candidate_id, criteria)
        else:  # a resumed loop: the candidate was already decided
            summary["candidate"] = {"candidate_id": candidate_id, "state": record["state"]}
    return summary


def _unfinished(store, pod_id, snapshot_sha):
    rows = store.db.execute(
        "SELECT params_json FROM tasks WHERE pod_id=? AND state IN ('queued','leased','retry_wait')",
        (pod_id,),
    )
    return any(json.loads(r[0]).get("dataset_manifest_hash") == snapshot_sha for r in rows)


def _operation(store, pod_id):
    row = store.db.execute("SELECT role FROM pods WHERE pod_id=?", (pod_id,)).fetchone()
    return (PLANNED_OPERATIONS.get(row[0]) or (None,))[0] if row else None


def _plain(proposal: Proposal):
    return {"operation": proposal.operation, "params": proposal.params}


def _brief(record):
    return {
        "round": record["round"],
        "accepted": record["accepted"],
        "rejected": [r["reason"] for r in record["rejected"]],
        "new_task_ids": record["new_task_ids"],
        "planner_stop": record["planner_stop"],
    }
