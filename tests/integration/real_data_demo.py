"""Real-data demonstration through the operator CLI (roadmap H). Not part of the suite.

Inputs are real but historical: a registry dataset built from the public Kalshi
historical listings and one-minute candles (retrieved after the fact) joined to the
official MLB schedule. The final holdout is never read here.

Phase `pods`: snapshot -> research pods (logistic, gradient boosting, symbolic search)
-> accept -> protected evaluator under the default criteria -> robustness pod on every
candidate. Phase `backtest` (needs the execution-only `hist-candles-post` stage for the
validation rows): execution snapshot -> paper-backtest pods, and a coordinator-run
validation backtest grid per candidate. `--coordinator single` replaces the grid with
one ledger per candidate and assumed depth (default fee schedule, latency and slippage),
run as parallel processes, and `--skip-pod-backtest` skips the pods: a paper run rebuilds
the ledger projection after every write, so its cost grows with the square of the
validation rows and the full grid is impractical on a two-season validation set.

Every command is recorded with its arguments and a summary of its output. A validated
candidate would still not be evidence of edge: validation compares several proposals.

Usage:
  .venv/bin/python tests/integration/real_data_demo.py pods --registry R --dataset D \
      --root ORCH --out OUT.json
  .venv/bin/python tests/integration/real_data_demo.py backtest --registry R --dataset D \
      --root ORCH --history-db H --out OUT.json [--coordinator single --skip-pod-backtest]
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from vader_intelligence.cli import execute, parser  # noqa: E402

SPLIT = ["--development-through", "2026-07-31", "--validation-through", "2026-08-31"]


def set_split(development_through, validation_through):
    SPLIT[1], SPLIT[3] = development_through, validation_through


class Recorder:
    def __init__(self, out):
        self.out = Path(out)
        self.log = json.loads(self.out.read_text()) if self.out.exists() else {"steps": []}

    def __call__(self, *argv, keep=None):
        result = execute(parser().parse_args(list(argv)))
        brief = result if keep is None else keep(result)
        self.log["steps"].append({"argv": ["vader", *argv], "result": brief})
        self.out.write_text(json.dumps(self.log, indent=2, sort_keys=True, default=str) + "\n")
        return result

    def parallel(self, argvs, *, keep, jobs):
        """Run CLI commands as separate processes (the simulator is CPU-bound); record
        them in the given order with their wall time."""

        def one(argv):
            started = time.monotonic()
            done = subprocess.run(
                [sys.executable, "-m", "vader_intelligence.cli", *argv],
                capture_output=True,
                text=True,
                env=os.environ | {"PYTHONPATH": str(ROOT / "src")},
                check=False,
            )
            if done.returncode:
                raise RuntimeError(f"{argv[:2]} failed: {done.stderr[-2000:]}")
            return json.loads(done.stdout), round(time.monotonic() - started, 1)

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for argv, (result, seconds) in zip(argvs, pool.map(one, argvs), strict=True):
                brief = keep(result) | {"wall_seconds": seconds}
                self.log["steps"].append({"argv": ["vader", *argv], "result": brief})
        self.out.write_text(json.dumps(self.log, indent=2, sort_keys=True, default=str) + "\n")


def pods_phase(args, run):
    root, reg = ["--root", args.root], ["--registry", args.registry]
    run("pods", "init", *root)
    for pod, role in (
        ("baseline", "baseline_models"),
        ("symbolic", "symbolic_search"),
        ("robust", "robustness_execution"),
    ):
        run("pods", "register", *root, "--pod", pod, "--role", role, "--wall-seconds", "1800")
    snap = run("pods", "snapshot", *root, *reg, "--dataset", args.dataset, *SPLIT)
    sha = snap["snapshot_sha256"]
    base = {
        "dataset_manifest_hash": sha,
        "feature_set": snap["feature_set"],
        "feature_set_version": snap["feature_set_version"],
        "seed": 0,
    }
    tasks = {
        "logistic": ("baseline", "train_model", base | {"model_family": "logistic"}),
        "gradient_boosting": (
            "baseline",
            "train_model",
            base | {"model_family": "gradient_boosting"},
        ),
        "symbolic": (
            "symbolic",
            "symbolic_search",
            {
                "dataset_manifest_hash": sha,
                "seed": 0,
                "max_candidates": 20000,
                "max_generations": 30,
                "max_seconds": 300.0,
            },
        ),
    }
    ids = {}
    for name, (pod, operation, params) in tasks.items():
        ids[name] = run(
            "pods",
            "submit",
            *root,
            "--pod",
            pod,
            "--operation",
            operation,
            "--params",
            json.dumps(params),
        )["task_id"]
    run(
        "pods",
        "run",
        *root,
        "--deadline-seconds",
        "3600",
        keep=lambda r: {
            "run": r["run"],
            "tasks_by_state": r["pods"]["tasks_by_state"],
            "recent_failures": r["pods"]["recent_failures"],
        },
    )
    candidates = {}
    for name, task in ids.items():
        accepted = run(
            "pods",
            "accept",
            *root,
            *reg,
            "--task",
            task,
            keep=lambda r: {
                "candidate_id": r["candidate"]["candidate_id"],
                "spec": r["candidate"]["spec"],
            },
        )
        candidates[name] = accepted["candidate"]["candidate_id"]
    for name, candidate in candidates.items():
        run(
            "pods",
            "evaluate",
            *root,
            *reg,
            "--candidate",
            candidate,
            keep=lambda r: {k: r[k] for k in ("state", "recomputed", "proposals")},
        )
        run(
            "pods",
            "submit",
            *root,
            "--pod",
            "robust",
            "--operation",
            "robustness_analysis",
            "--params",
            json.dumps(
                {
                    "candidate_id": candidate,
                    "dataset_manifest_hash": sha,
                    "perturbations": [
                        "by_month",
                        "regular_season_only",
                        "tight_spread",
                        "wide_spread",
                        "drop_feature:mid_c",
                    ],
                    "seed": 0,
                }
            ),
        )
    run(
        "pods",
        "run",
        *root,
        "--deadline-seconds",
        "1800",
        keep=lambda r: {
            "run": r["run"],
            "tasks_by_state": r["pods"]["tasks_by_state"],
            "recent_failures": r["pods"]["recent_failures"],
        },
    )
    return candidates


def backtest_phase(args, run):
    root, reg = ["--root", args.root], ["--registry", args.registry]
    snap = run("pods", "snapshot", *root, *reg, "--dataset", args.dataset, *SPLIT)
    execution = run(
        "pods",
        "execution-snapshot",
        *root,
        *reg,
        "--dataset",
        args.dataset,
        "--history-db",
        args.history_db,
        *SPLIT,
    )
    rows = json.loads(Path(args.root, "candidates.json").read_text())
    if not args.skip_pod_backtest:
        pod_backtests(args, run, rows, snap, execution)
    if args.coordinator == "single":
        single_backtests(args, run, rows)
    else:
        grid_backtests(args, run, rows)


ENVELOPE = ("segment", "selection_biased", "rows", "forecasts", "period", "skipped")


def pod_backtests(args, run, rows, snap, execution):
    root = ["--root", args.root]
    for name, candidate in rows.items():
        run(
            "pods",
            "submit",
            *root,
            "--pod",
            "robust",
            "--operation",
            "paper_backtest",
            "--params",
            json.dumps(
                {
                    "candidate_id": candidate["candidate_id"],
                    "dataset_manifest_hash": snap["snapshot_sha256"],
                    "fee_schedule_version": "kalshi-2026-02-general",
                    "execution_inputs_hash": execution["execution_inputs_sha256"],
                    "assumed_depths": ["5", "25"],
                }
            ),
        )
    run(
        "pods",
        "run",
        *root,
        "--deadline-seconds",
        "3600",
        keep=lambda r: {
            "run": r["run"],
            "tasks_by_state": r["pods"]["tasks_by_state"],
            "recent_failures": r["pods"]["recent_failures"],
        },
    )


def grid_backtests(args, run, rows):
    for name, candidate in rows.items():
        run(
            "backtest",
            "run",
            "--registry",
            args.registry,
            "--history-db",
            args.history_db,
            "--dataset",
            args.dataset,
            "--model",
            candidate["model_sha256"],
            *SPLIT,
            "--segment",
            "validation",
            "--assumed-depth",
            "5",
            "--assumed-depth",
            "25",
            keep=lambda r: (
                {k: r[k] for k in ENVELOPE}
                | {
                    "markets_with_execution_data": r["markets_with_execution_data"],
                    "scenarios": [
                        {
                            "scenario": s["scenario"],
                            "orders": s["orders"],
                            "fills": s["fills"],
                            "pnl": s["pnl"],
                            "rejections_by_reason": s["rejections_by_reason"],
                            "unidentifiable": s["unidentifiable"],
                            "verification_failures": s["verification_failures"],
                        }
                        for s in r["report"]["rows"]
                    ],
                }
            ),
        )


def single_backtests(args, run, rows):
    ledgers = Path(args.root, "backtest-ledgers")
    ledgers.mkdir(exist_ok=True)
    argvs = [
        [
            "backtest",
            "run",
            "--registry",
            args.registry,
            "--history-db",
            args.history_db,
            "--dataset",
            args.dataset,
            "--model",
            candidate["model_sha256"],
            *SPLIT,
            "--segment",
            "validation",
            "--assumed-depth",
            depth,
            "--ledger",
            str(ledgers / f"{name}-depth-{depth}.sqlite3"),
        ]
        for name, candidate in rows.items()
        for depth in ("5", "25")
    ]

    def keep(r):
        report = r["report"]
        return (
            {k: r[k] for k in ENVELOPE}
            | {"markets_with_execution_data": r["markets_with_execution_data"]}
            | {
                "model_id": r["model_id"],
                "scenario": {
                    "fee_schedule": report["manifest"]["fee_schedule"]["version"],
                    "fee_verified": report["manifest"]["fee_schedule"]["verified"],
                    "assumed_depth": report["manifest"]["config"]["assumed_depth"],
                    "extra_slippage_ticks": report["manifest"]["config"]["extra_slippage_ticks"],
                    "latency": report["manifest"]["config"]["latency"],
                },
                "orders": len(report["orders"]),
                "fills": len(report["fills"]),
                "pnl": report["pnl"],
                "rejections_by_reason": report["rejections_by_reason"],
                "forecasts_without_intent": report["forecasts_without_intent"],
                "unidentifiable": report["unidentifiable"],
                "verification": report["verification"],
            }
        )

    run.parallel(argvs, keep=keep, jobs=args.jobs)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=("pods", "backtest"))
    p.add_argument("--registry", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--history-db")
    p.add_argument("--out", required=True)
    p.add_argument("--development-through", default="2026-07-31")
    p.add_argument("--validation-through", default="2026-08-31")
    p.add_argument("--coordinator", choices=("grid", "single"), default="grid")
    p.add_argument("--skip-pod-backtest", action="store_true")
    p.add_argument("--jobs", type=int, default=6)
    args = p.parse_args()
    set_split(args.development_through, args.validation_through)
    run = Recorder(args.out)
    if args.phase == "pods":
        candidates = pods_phase(args, run)
        from vader_intelligence.pods.candidates import candidate_record
        from vader_intelligence.pods.store import open_store

        store = open_store(args.root)
        try:
            listing = {
                name: {
                    "candidate_id": cid,
                    "model_sha256": candidate_record(store, cid)["spec"]["model_sha256"],
                    "state": candidate_record(store, cid)["state"],
                }
                for name, cid in candidates.items()
            }
        finally:
            store.close()
        Path(args.root, "candidates.json").write_text(json.dumps(listing, indent=2) + "\n")
        print(json.dumps(listing, indent=2))
    else:
        if not args.history_db:
            raise SystemExit("--history-db is required for the backtest phase")
        backtest_phase(args, run)


if __name__ == "__main__":
    main()
