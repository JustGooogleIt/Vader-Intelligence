"""Real-data demonstration through the operator CLI (roadmap H). Not part of the suite.

Inputs are real but historical: a registry dataset built from the public Kalshi
historical listings and one-minute candles (retrieved after the fact) joined to the
official MLB schedule. The final holdout is never read here.

Phase `pods`: snapshot -> research pods (logistic, gradient boosting, symbolic search)
-> accept -> protected evaluator under the default criteria -> robustness pod on every
candidate. Phase `backtest` (needs the execution-only `hist-candles-post` stage for the
validation rows): execution snapshot -> paper-backtest pods, and a coordinator-run
validation backtest grid per candidate.

Every command is recorded with its arguments and a summary of its output. A validated
candidate would still not be evidence of edge: validation compares several proposals.

Usage:
  .venv/bin/python tests/integration/real_data_demo.py pods --registry R --dataset D \
      --root ORCH --out OUT.json
  .venv/bin/python tests/integration/real_data_demo.py backtest --registry R --dataset D \
      --root ORCH --history-db H --out OUT.json
"""

import argparse
import json
import sys
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
                {
                    k: r[k]
                    for k in (
                        "segment",
                        "selection_biased",
                        "rows",
                        "forecasts",
                        "period",
                        "skipped",
                    )
                }
                | {
                    "markets_with_execution_data": r["markets_with_execution_data"],
                    "scenarios": [
                        {
                            "scenario": s["scenario"],
                            "orders": s["orders"],
                            "fills": s["fills"],
                            "pnl": s["pnl"],
                            "unidentifiable": s["unidentifiable"],
                        }
                        for s in r["report"]["rows"]
                    ],
                }
            ),
        )


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
