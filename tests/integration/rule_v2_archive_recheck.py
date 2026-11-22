"""Read-only recheck of archived eligibility decisions under rule v1 and rule v2 (D-002).

For an existing archive (opened `mode=ro`, never written), samples archived eligibility
checks and recomputes `scope.eligible` from the exact archived event and schedule
response bytes, the archived schedule retrieval time and the original check time. It
reports, per event ticker, what rule v1 and rule v1+v2 decide. This is a historical
recheck of stored evidence, not forward availability: nothing here was witnessed, no
receipt or decision is written, and an `eligible` here does not mean a forecast could
have been published.

Usage: .venv/bin/python tests/integration/rule_v2_archive_recheck.py --db ARCHIVE
           [--per-ticker 25] [--since 2026-10-08]
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from vader_intelligence.config import Config  # noqa: E402
from vader_intelligence.normalize import decode  # noqa: E402
from vader_intelligence.scope import eligible, ticker_identity  # noqa: E402


def body(db, fetch_id, cache):
    if fetch_id not in cache:
        row = db.execute(
            "SELECT b.body, b.sha256, f.retrieved_at FROM fetches f JOIN blobs b"
            " ON b.sha256=f.body_sha256 WHERE f.id=?",
            (fetch_id,),
        ).fetchone()
        if row is None or hashlib.sha256(row[0]).hexdigest() != row[1]:
            raise ValueError(f"missing or corrupt archived body for {fetch_id}")
        cache[fetch_id] = (decode(row[0]), row[2])
        if len(cache) > 256:
            cache.pop(next(iter(cache)))
    return cache[fetch_id]


def sample(rows, per_ticker):
    """Evenly spaced checks per ticker, always including the first and the last."""
    if len(rows) <= per_ticker:
        return rows
    step = (len(rows) - 1) / (per_ticker - 1)
    return [rows[round(i * step)] for i in range(per_ticker)]


def main():
    options = argparse.ArgumentParser()
    options.add_argument("--db", required=True)
    options.add_argument("--per-ticker", type=int, default=25)
    options.add_argument("--since", default="2026-10-08")
    options.add_argument("--reasons", default="unrecognized_full_game_rule,eligible")
    args = options.parse_args()
    db = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    reasons = tuple(args.reasons.split(","))
    by_ticker = defaultdict(list)
    for row in db.execute(
        "SELECT id, ticker, checked_at, reason, market_fetch_id, schedule_fetch_id"
        " FROM eligibility WHERE checked_at>=? AND reason IN (%s) AND market_fetch_id IS NOT"
        " NULL AND schedule_fetch_id IS NOT NULL ORDER BY id" % ",".join("?" for _ in reasons),
        (args.since, *reasons),
    ):
        by_ticker[row[1]].append(row)
    v1 = Config(database="unused.sqlite3")
    v2 = Config(database="unused.sqlite3", eligibility_rules=("rule-v1", "rule-v2"))
    cache, report, totals = {}, {}, {"v1": Counter(), "v2": Counter()}
    corroboration = Counter()
    for ticker, rows in sorted(by_ticker.items()):
        per = {"v1": Counter(), "v2": Counter()}
        archived = Counter(r[3] for r in rows)
        starts = set()
        for _, _, checked_at, _, market_fetch, schedule_fetch in sample(rows, args.per_ticker):
            event_data, _ = body(db, market_fetch, cache)
            schedule, schedule_at = body(db, schedule_fetch, cache)
            market = next(m for m in event_data["markets"] if m["ticker"] == ticker)
            now = datetime.fromisoformat(checked_at)
            for name, config in (("v1", v1), ("v2", v2)):
                result = eligible(
                    market,
                    event_data["event"],
                    event_data["markets"],
                    schedule,
                    schedule_at,
                    now,
                    config,
                )
                per[name][result.reason] += 1
                totals[name][result.reason] += 1
                if name == "v2" and result.scheduled_start:
                    starts.add(result.scheduled_start)
                    ticker_start = ticker_identity(event_data["event"]["event_ticker"])["start"]
                    corroboration[
                        datetime.fromisoformat(result.scheduled_start) == ticker_start
                    ] += 1
        report[ticker] = {
            "archived_checks": len(rows),
            "archived_reasons": dict(archived),
            "sampled": sum(per["v1"].values()),
            "v1": dict(per["v1"]),
            "v1_plus_v2": dict(per["v2"]),
            "v2_official_starts": sorted(starts),
        }
    print(
        json.dumps(
            {
                "archive": str(Path(args.db).resolve()),
                "opened": "read-only (mode=ro, query_only)",
                "evidence_kind": "historical recheck of archived bytes; not forward evidence",
                "since": args.since,
                "per_ticker": args.per_ticker,
                "tickers": len(report),
                "totals": {"v1": dict(totals["v1"]), "v1_plus_v2": dict(totals["v2"])},
                "v2_start_equals_ticker_time": {str(k): v for k, v in corroboration.items()},
                "by_ticker": report,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
