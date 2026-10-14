"""Deterministic disposable-data demonstration: collection → evaluation → reports.

Runs entirely offline on a temporary archive with the shipped synthetic fixture, an
injected transport and injected clocks. Prints the forward-shadow and
cutoff-reconstruction reports and checks them against hand-computed expectations. A
reviewer can verify every number below by hand:

- The fixture book for KXMLBGAME-26SEP271505HOUATH-HOU has best YES bid 0.6400 and best
  NO bid 0.3500, so YES ask = 1 − 0.3500 = 0.6500 and midpoint = (0.6400 + 0.6500) / 2
  = 0.6450. Constant is 0.5.
- The synthetic settlement finalizes YES (y = 1, payout 1.0000).
- Brier: constant (0.5 − 1)² = 0.25; midpoint (0.6450 − 1)² = 0.12602500;
  delta = −0.12397500.
- Log loss: constant −ln(0.5) = 0.693147…; midpoint −ln(0.6450) = 0.438505…;
  delta = −0.254642….
- One opportunity: U = E = K = M = P = 1; published timely so FK = FM = FP = 1.

Usage: .venv/bin/python tests/integration/research_demo.py [--keep DIR]
"""

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in ("tests", "tests/forecast", "src"):
    sys.path.insert(0, str(ROOT / extra))

from conftest import Clock, response  # noqa: E402
from test_collector import handler_for  # noqa: E402
from test_transport import reader_for  # noqa: E402

from vader_intelligence.cli import execute, parser  # noqa: E402
from vader_intelligence.collector import Collector  # noqa: E402
from vader_intelligence.config import Config  # noqa: E402
from vader_intelligence.forecast.research import Protocol  # noqa: E402
from vader_intelligence.forecast.tick import tick  # noqa: E402
from vader_intelligence.research import report as reports  # noqa: E402
from vader_intelligence.research.runner import run as research_run  # noqa: E402
from vader_intelligence.settlement import journal as settlement_journal  # noqa: E402
from vader_intelligence.settlement.service import SettlementRefresh  # noqa: E402
from vader_intelligence.storage import Store  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/mlb.json"
TICKER = "KXMLBGAME-26SEP271505HOUATH-HOU"
EVENT = "KXMLBGAME-26SEP271505HOUATH"
PDF = b"%PDF-1.4\nsynthetic fixture\n%%EOF"
GAME_DAY = date(2026, 9, 27)
EXPECTED = {
    "midpoint": Decimal("0.6450"),
    "brier_constant": Decimal("0.25"),
    "brier_midpoint": Decimal("0.126025"),
    "brier_delta": Decimal("-0.123975"),
    "log_loss_constant_prefix": "0.69314718",
    "log_loss_midpoint_prefix": "0.43850496",
}


def at(hour, minute, second=0):
    return datetime(2026, 9, 27, hour, minute, second, tzinfo=timezone.utc)


def clock_at(moment):
    clock = Clock()
    clock.origin = moment
    return clock


def settlement_handler():
    data = json.loads(FIXTURE.read_text())
    event_body = json.loads(next(r["body"] for r in data["responses"] if r["stage"] == "event"))
    schedule = json.loads(next(r["body"] for r in data["responses"] if r["stage"] == "schedule"))
    market = next(m for m in event_body["markets"] if m["ticker"] == TICKER) | {
        "status": "finalized",
        "result": "yes",
        "settlement_value_dollars": "1.0000",
        "settlement_ts": "2026-09-27T22:30:00Z",
        "updated_time": "2026-09-27T22:30:01Z",
    }
    game = schedule["dates"][0]["games"][0]
    game["status"] = {"abstractGameState": "Final", "detailedState": "Final", "startTimeTBD": False}
    game["isTie"] = False
    game["teams"]["away"].update(isWinner=True, score=5)
    game["teams"]["home"].update(isWinner=False, score=2)

    def handler(request):
        path = request.url.path
        if path.endswith("/series/KXMLBGAME"):
            return response(
                data={
                    "series": {
                        "ticker": "KXMLBGAME",
                        "contract_terms_url": settlement_journal.TERMS_URL,
                    }
                }
            )
        if path.endswith(".pdf"):
            return response(body=PDF)
        if path.endswith("/markets/" + TICKER):
            return response(data={"market": market})
        if path.endswith("/events/" + EVENT):
            return response(data={"event": event_body["event"]})
        if path.endswith("/schedule"):
            return response(data=schedule)
        raise AssertionError(request.url)

    return handler


def main():
    cli_args = argparse.ArgumentParser(description=__doc__)
    cli_args.add_argument("--keep", help="keep the disposable archive in this new directory")
    options = cli_args.parse_args()
    settlement_journal.REVIEWED_TERMS = {hashlib.sha256(PDF).hexdigest()}
    protocol = Protocol(dataset="demo-forward")
    checks = []

    def check(name, actual, expected):
        ok = actual == expected
        checks.append({"check": name, "actual": str(actual), "expected": str(expected), "ok": ok})
        if not ok:
            raise SystemExit(f"demo expectation failed: {name}: {actual!r} != {expected!r}")

    with tempfile.TemporaryDirectory(prefix="vader-research-demo-") as temporary:
        root = Path(options.keep) if options.keep else Path(temporary)
        if options.keep:
            root.mkdir(parents=True, exist_ok=False)
        path = root / "demo.sqlite3"
        config = Config(database=str(path), min_free_bytes=1)

        def cli(*argv):
            return execute(parser().parse_args(["--db", str(path), *argv]))

        for argv in (
            ("db-init",),
            ("settlement", "migrate"),
            ("forecast", "migrate"),
            ("forecast", "migrate", "--record-contract", "2"),
        ):
            cli(*argv)
        store = Store(path, min_free_bytes=1)
        try:
            handler, _, _ = handler_for(json.loads(FIXTURE.read_text()))
            collected = Collector(
                store, reader_for(store, config, handler, clock_at(at(18, 3))), config
            ).run()
            check(
                "collection complete with two books",
                (collected["status"], collected["books"]),
                ("complete", 2),
            )
            warm = clock_at(at(18, 3, 30))
            check(
                "warm tick prepares one binding",
                tick(store, protocol, clock=warm.now, monotonic=warm.mono)["bindings_prepared"],
                1,
            )
            due = clock_at(at(18, 5, 30))
            due_result = tick(store, protocol, clock=due.now, monotonic=due.mono)
            check(
                "due tick publishes one eligible decision",
                (due_result["decisions_eligible"], due_result["publications"]["allowed"]),
                (1, 1),
            )
            decision = json.loads(
                store.db.execute("SELECT payload_json FROM forecast_decisions").fetchone()[0]
            )
            check(
                "midpoint probability",
                Decimal(decision["results"]["midpoint-v1"]["probability"]),
                EXPECTED["midpoint"],
            )
            reader = reader_for(store, config, settlement_handler(), clock_at(at(23, 0)))
            try:
                settled = SettlementRefresh(store, reader, config).run(tickers=[TICKER])
            finally:
                reader.close()
            check("settlement refresh complete", settled["status"], "complete")
            runner_clock = clock_at(at(23, 40))
            forward = research_run(
                store,
                protocol,
                view="forward-shadow",
                outcomes_as_of=at(23, 30),
                start_date=GAME_DAY,
                through_date=GAME_DAY,
                clock=runner_clock.now,
                monotonic=runner_clock.mono,
            )
            study = research_run(
                store,
                protocol,
                view="cutoff-reconstruction",
                dataset="demo-study",
                outcomes_as_of=at(23, 30),
                start_date=GAME_DAY,
                through_date=GAME_DAY,
                clock=runner_clock.now,
                monotonic=runner_clock.mono,
            )
            texts = {}
            for label, summary in (("forward-shadow", forward), ("cutoff-reconstruction", study)):
                (cohort,) = summary["segments"]["whole"]["cohorts"].values()
                paired = cohort["paired"]
                check(f"{label} status", summary["status"], "complete")
                check(f"{label} paired n", paired["n"], 1)
                check(
                    f"{label} Brier constant",
                    Decimal(paired["mean_brier_constant"]),
                    EXPECTED["brier_constant"],
                )
                check(
                    f"{label} Brier midpoint",
                    Decimal(paired["mean_brier_midpoint"]),
                    EXPECTED["brier_midpoint"],
                )
                check(
                    f"{label} Brier delta",
                    Decimal(paired["mean_brier_delta"]),
                    EXPECTED["brier_delta"],
                )
                check(
                    f"{label} log loss constant prefix",
                    paired["mean_log_loss_constant"][:10],
                    EXPECTED["log_loss_constant_prefix"],
                )
                check(
                    f"{label} log loss midpoint prefix",
                    paired["mean_log_loss_midpoint"][:10],
                    EXPECTED["log_loss_midpoint_prefix"],
                )
                check(
                    f"{label} population",
                    {k: cohort["population"][k] for k in "UEKMP"},
                    {k: 1 for k in "UEKMP"},
                )
                texts[label] = reports.text_report(store.db, cohort["evaluation_id"])
            (fcohort,) = forward["segments"]["whole"]["cohorts"].values()
            check(
                "forward publication coverage",
                {k: fcohort["population"][k] for k in ("FK", "FM", "FP")},
                {"FK": 1, "FM": 1, "FP": 1},
            )
            check("archive integrity", store.verify_integrity(), [])
        finally:
            store.close()
        replayed = cli("replay")
        check(
            "replay preserves research rows",
            replayed["forecast_storage"]["replay"],
            "preserved_not_reconstructed",
        )
        for label, text in texts.items():
            print(f"=== {label} ===")
            print(text)
            print()
        print(
            json.dumps(
                {
                    "status": "complete",
                    "synthetic": True,
                    "checks": checks,
                    "archive": str(path) if options.keep else "disposable (deleted)",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
