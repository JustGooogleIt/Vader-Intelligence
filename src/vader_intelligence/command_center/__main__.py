"""`python -m vader_intelligence.command_center serve|controller|measure`."""

import argparse
import json
import statistics
import sys
import time
import urllib.request

from .config import CenterConfig


def parser():
    p = argparse.ArgumentParser(prog="python -m vader_intelligence.command_center")
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="loopback dashboard server (read API + command intake)")
    serve.add_argument("--config", required=True, help="command center JSON configuration")
    serve.add_argument("--port", type=int)
    ctl = sub.add_parser(
        "controller", help="apply commands and dispatch farm stages (one per root)"
    )
    ctl.add_argument("--config", required=True)
    ctl.add_argument("--max-steps", type=int, default=10**6)
    ctl.add_argument("--deadline-seconds", type=float, default=12 * 3600)
    ctl.add_argument("--stop-when-idle", action="store_true")
    measure = sub.add_parser("measure", help="measure read latency of a running server")
    measure.add_argument("--url", default="http://127.0.0.1:8765")
    measure.add_argument("--n", type=int, default=50)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "serve":
        from .server import serve

        cfg = CenterConfig.load(args.config)
        if args.port:
            cfg = CenterConfig(**{**cfg.__dict__, "port": args.port})
        from .panes import pane_reader

        print(
            json.dumps(
                {
                    "serving": f"http://{cfg.host}:{cfg.port}/",
                    "root": str(cfg.root),
                    "archive": str(cfg.archive),
                }
            ),
            flush=True,
        )
        serve(cfg, pane_reader=pane_reader)
        return 0
    if args.command == "controller":
        from .controller import run_controller

        cfg = CenterConfig.load(args.config)
        report = run_controller(
            cfg,
            max_steps=args.max_steps,
            deadline_seconds=args.deadline_seconds,
            stop_when_idle=args.stop_when_idle,
        )
        print(json.dumps(report, sort_keys=True), flush=True)
        return 0
    if args.command == "measure":
        samples = []
        for _ in range(args.n):
            started = time.perf_counter()
            with urllib.request.urlopen(
                args.url.rstrip("/") + "/api/overview", timeout=10
            ) as response:
                response.read()
            samples.append(time.perf_counter() - started)
        samples.sort()
        report = {
            "n": len(samples),
            "p50_ms": round(statistics.median(samples) * 1000, 1),
            "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))] * 1000, 1),
            "max_ms": round(samples[-1] * 1000, 1),
        }
        print(json.dumps(report), flush=True)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
