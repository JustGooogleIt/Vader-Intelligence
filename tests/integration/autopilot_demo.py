"""Pod autopilot on a real registry snapshot (roadmap G). Not part of the suite.

Registers one baseline-models pod and one symbolic-search pod on an existing
orchestration root, lets the heuristic planner choose and run approved operations in
bounded rounds on the snapshot's development rows, and hands each pod's best model by
development score to the protected evaluator (one candidate per pod). A vendor-planner
round shows the paid-call boundary. The holdout is never read.

Every command is recorded with its arguments and its output. A validated candidate would
still not be evidence of edge: validation compares several proposals on one snapshot.

Usage:
  .venv/bin/python tests/integration/autopilot_demo.py --root ORCH --registry R \
      --snapshot SHA --out OUT.json [--prefix auto]
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from vader_intelligence.cli import execute, parser  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--prefix", default="auto")
    p.add_argument("--deadline-seconds", default="5400")
    args = p.parse_args()
    out = Path(args.out)
    log = json.loads(out.read_text()) if out.exists() else {"steps": []}

    def run(*argv):
        started = time.monotonic()
        result = execute(parser().parse_args(list(argv)))
        seconds = round(time.monotonic() - started, 1)
        log["steps"].append({"argv": ["vader", *argv], "wall_seconds": seconds, "result": result})
        out.write_text(json.dumps(log, indent=2, sort_keys=True, default=str) + "\n")
        return result

    root = ["--root", args.root]
    pods = {
        f"{args.prefix}-baseline": ("baseline_models", "4", "8"),
        f"{args.prefix}-symbolic": ("symbolic_search", "3", "3"),
    }
    for pod, (role, _, _) in pods.items():
        run("pods", "register", *root, "--pod", pod, "--role", role, "--wall-seconds", "1800")
    run(
        "pods",
        "autopilot",
        *root,
        "--pod",
        f"{args.prefix}-baseline",
        "--snapshot",
        args.snapshot,
        "--planner",
        "vendor",
        "--vendor",
        "any-vendor",
    )
    for pod, (_, rounds, max_tasks) in pods.items():
        run(
            "pods",
            "autopilot",
            *root,
            "--registry",
            args.registry,
            "--pod",
            pod,
            "--snapshot",
            args.snapshot,
            "--rounds",
            rounds,
            "--max-tasks",
            max_tasks,
            "--deadline-seconds",
            args.deadline_seconds,
            "--accept-best",
        )


if __name__ == "__main__":
    main()
