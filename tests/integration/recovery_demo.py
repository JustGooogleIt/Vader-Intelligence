"""Recovery demonstrations across the integrated system (roadmap H). SYNTHETIC data.

1. Research pod worker killed with SIGKILL mid-task: the coordinator records the attempt
   lost, retries, and the retried model artifact has the same digest as a clean run.
2. Coordinator killed with SIGKILL while its worker runs: a new coordinator recovers the
   attempt from the database alone; exactly one result is accepted.
3. Paper backtest crashed at durable steps: each resumed run reproduces the clean run's
   ledger projections and report.
4. Live adapter (against the synthetic exchange double): the create request is processed
   but its response is lost; the order is `unknown`, nothing is resubmitted, and
   reconciliation finds exactly one venue order.

Usage: .venv/bin/python tests/integration/recovery_demo.py [--keep DIR]
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in ("tests", "tests/trading", "tests/trading/kalshi", "tests/integration", "src"):
    sys.path.insert(0, str(ROOT / extra))

import e2e_demo  # noqa: E402
from _kalshi_double import PREFIX, Harness  # noqa: E402
from _trading_synthetic import TICKER, D, at, book, make_intent, market  # noqa: E402

from vader_intelligence.pods import evaluator  # noqa: E402
from vader_intelligence.pods.contracts import Budgets, PodSpec  # noqa: E402
from vader_intelligence.pods.coordinator import Coordinator, CoordinatorConfig  # noqa: E402
from vader_intelligence.pods.snapshot import feature_set_version, load  # noqa: E402
from vader_intelligence.pods.store import open_store  # noqa: E402
from vader_intelligence.research import backtest  # noqa: E402
from vader_intelligence.research.registry import Registry  # noqa: E402
from vader_intelligence.trading.risk import ForecastStatus  # noqa: E402
from vader_intelligence.trading.simulator import PaperConfig, run_paper  # noqa: E402

FAST = CoordinatorConfig(
    heartbeat_seconds=0.2,
    lease_seconds=2.0,
    backoff_base_seconds=0.1,
    backoff_max_seconds=0.5,
    poll_seconds=0.05,
)


class Crash(Exception):
    pass


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_claimed(store, task_id, n=1, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = store.attempts(task_id)
        if len(rows) >= n and rows[n - 1]["worker_pid"] is not None:
            return rows[n - 1]["worker_pid"]
        time.sleep(0.02)
    raise RuntimeError("worker never claimed its attempt")


def _train_task(store, registry, dataset_id, manifest):
    store.register_pod(PodSpec("baseline", "baseline_models", Budgets(wall_seconds_per_task=600)))
    snapshot = evaluator.snapshot_dataset(store, registry, dataset_id, e2e_demo.SPLIT)
    params = {
        "dataset_manifest_hash": snapshot,
        "feature_set": "fs-market-v1",
        "feature_set_version": feature_set_version(manifest),
        "model_family": "gradient_boosting",
        "seed": 0,
        "hyperparameters": {
            "grid": {
                "learning_rate": [0.05],
                "max_iter": [200],
                "max_leaf_nodes": [15],
                "min_samples_leaf": [40],
                "l2_regularization": [1.0],
            },
            "n_folds": 3,
        },
    }
    return e2e_demo.submit(store, "baseline", "train_model", params)


def _model_sha(store, task_id):
    artifacts = store.result(task_id)["result"]["artifacts"]
    return next(a["sha256"] for a in artifacts if a["kind"] == "model_artifact")


def worker_kill(workdir, registry, dataset_id, manifest):
    clean_store = open_store(workdir / "clean")
    clean_task = _train_task(clean_store, registry, dataset_id, manifest)
    with Coordinator(clean_store, FAST) as coordinator:
        coordinator.run(max_steps=100_000, deadline_seconds=600)
    clean_sha = _model_sha(clean_store, clean_task)
    clean_store.close()

    store = open_store(workdir / "killed")
    task = _train_task(store, registry, dataset_id, manifest)
    with Coordinator(store, FAST) as coordinator:
        coordinator.step()
        pid = _wait_claimed(store, task)
        os.kill(pid, signal.SIGKILL)
        coordinator.run(max_steps=100_000, deadline_seconds=600)
    attempts = [[a["outcome"], a["reason"]] for a in store.attempts(task)]
    out = {
        "attempts": attempts,
        "state": store.task(task)["state"],
        "model_sha256": _model_sha(store, task),
        "clean_model_sha256": clean_sha,
        "killed_pid_alive": _alive(pid),
    }
    store.close()
    return out


def coordinator_kill(workdir, registry, dataset_id, manifest):
    root = workdir / "coordinator-kill"
    store = open_store(root)
    task = _train_task(store, registry, dataset_id, manifest)
    script = (
        "import time\n"
        "from vader_intelligence.pods.coordinator import Coordinator, CoordinatorConfig\n"
        "from vader_intelligence.pods.store import open_store\n"
        f"c = Coordinator(open_store({str(root)!r}), CoordinatorConfig(heartbeat_seconds=0.2,"
        " lease_seconds=2.0, poll_seconds=0.05))\n"
        "c.step()\n"
        "print('spawned', flush=True)\n"
        "time.sleep(600)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    parent = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env
    )
    try:
        assert parent.stdout.readline().strip() == "spawned"
        pid = _wait_claimed(store, task)
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
        worker_survived = _alive(pid)
        with Coordinator(store, FAST) as coordinator:
            first = coordinator.step()
            coordinator.run(max_steps=100_000, deadline_seconds=600)
        out = {
            "worker_survived_coordinator_death": worker_survived,
            "recovered_active_on_first_step": first["active"],
            "attempts": [[a["outcome"], a["reason"]] for a in store.attempts(task)],
            "state": store.task(task)["state"],
            "results": store.db.execute(
                "SELECT COUNT(*) FROM results WHERE task_id=?", (task,)
            ).fetchone()[0],
            "worker_alive_after": _alive(pid),
        }
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        parent.stdout.close()
        store.close()
    return out


def backtest_crash(workdir, registry, dataset_id, model_id, rows):
    validation = rows[:30]
    execution = e2e_demo.synthetic_execution(validation)
    manifest = registry.dataset(dataset_id)
    forecasts = backtest.forecasts(
        registry.model_artifact(model_id), manifest, validation, model_id
    )
    inputs = backtest.paper_inputs(execution, forecasts, "synthetic:recovery")
    config = PaperConfig(assumed_depth=Decimal(10))
    points = []
    clean = run_paper(config, inputs, workdir / "bt-clean.sqlite3", fault=points.append).to_dict()
    chosen = sorted({0, len(points) // 4, len(points) // 2, 3 * len(points) // 4, len(points) - 1})
    resumed = []
    for index in chosen:
        calls = []

        def fault(name, index=index):
            calls.append(name)
            if len(calls) == index + 1:
                raise Crash(name)

        path = workdir / f"bt-crash-{index}.sqlite3"
        try:
            run_paper(config, inputs, path, fault=fault)
            crashed = False
        except Crash:
            crashed = True
        report = run_paper(config, inputs, path).to_dict()
        resumed.append(
            {
                "crash_point": index,
                "step": points[index],
                "crashed": crashed,
                # The whole report (orders, fills, P&L, exposure history, rejections,
                # settlements, verification), not only a few fields.
                "identical": {k: v for k, v in report.items() if k != "manifest"}
                == {k: v for k, v in clean.items() if k != "manifest"},
                "verification": report["verification"],
            }
        )
    return {"durable_steps": len(points), "fills": len(clean["fills"]), "resumed": resumed}


def live_unknown(workdir):
    h = Harness(workdir / "live-double.sqlite3")
    h.double.inject("POST", PREFIX + "/portfolio/events/orders", "timeout_after")
    now = h.at(10)
    intent = make_intent(t=10, probability="0.55", tif="gtc")
    kwargs = dict(
        now=now,
        market=market(),
        snapshot=book(t=5),
        forecast=ForecastStatus(TICKER, D("0.55"), at(5), "synthetic-model"),
        marks={TICKER: book(t=5)},
        equity_history=((at(-30), D(1000)),),
    )
    placement = h.executor.place(intent, **kwargs)
    coid = placement.order.client_order_id
    status_after_timeout = h.ledger.projection().orders[coid].status
    again = h.executor.place(intent, **kwargs)
    report = h.executor.reconcile(now=h.at(12))
    resolved = h.ledger.projection().orders[coid]
    out = {
        "submit_status": placement.submit.status,
        "order_status_after_timeout": status_after_timeout,
        "same_intent_again_existing": again.existing,
        "reconciliation_clean": report.clean,
        "resolved_status": resolved.status,
        "venue_orders_with_client_id": len(h.double.orders_with(coid)),
        "create_requests": len(h.double.calls_to("POST", "/portfolio/events/orders")),
        "ledger_verification": h.ledger.verify(),
    }
    h.ledger.close()
    return out


def run(workdir):
    workdir = Path(workdir)
    registry = Registry(workdir / "research")
    try:
        manifest, rows = e2e_demo.demo_dataset()
        dataset_id = registry.register_dataset(manifest, rows)
        out = {"notice": "SYNTHETIC recovery demonstrations; no live data or orders"}
        out["pod_worker_sigkill"] = worker_kill(workdir, registry, dataset_id, manifest)
        out["coordinator_sigkill"] = coordinator_kill(workdir, registry, dataset_id, manifest)
        store = open_store(workdir / "clean")
        try:
            task = store.db.execute("SELECT task_id FROM results").fetchone()[0]
            model_id = _model_sha(store, task)
            accepted = evaluator.accept_model(store, registry, task)
            snapshot = load(
                store.artifacts.get_bytes(
                    json.loads(store.task(task)["params_json"])["dataset_manifest_hash"]
                )
            )
        finally:
            store.close()
        out["backtest_crash_resume"] = backtest_crash(
            workdir, registry, dataset_id, model_id, snapshot.validation
        ) | {"candidate": accepted}
        out["live_adapter_unknown_submit"] = live_unknown(workdir)
    finally:
        registry.close()
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", help="keep the disposable data in this directory")
    args = parser.parse_args()
    if args.keep:
        Path(args.keep).mkdir(parents=True, exist_ok=True)
        result = run(args.keep)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            result = run(tmp)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
