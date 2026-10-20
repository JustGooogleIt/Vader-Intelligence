"""Candle-vs-collected-book check for the history assumption (roadmap B/H). Read-only.

The historical datasets and the paper backtest treat a one-minute candle's closing best
bid/ask as known at the candle's end and as a stand-in for the book. For games that are
in both archives this script compares, at each game's cutoff C, the candle-based features
(history archive) with the features computed from order books the production collector
actually retrieved before C (`datasets.collection`, quote source `book`). Both archives
are opened read-only (`mode=ro`); nothing is copied or written except the report.

Usage:
  .venv/bin/python tests/integration/candle_vs_book_check.py --production P \
      --history-db H --from 2026-09-30 --through 2026-10-31 --out OUT.json
"""

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from vader_intelligence.datasets import collection  # noqa: E402
from vader_intelligence.datasets import history as dataset_history  # noqa: E402

FEATURES = ("mid_c", "spread_c", "pair_mid_c", "last_trade_gap_c")
RULES = ("default", "history-date-only")


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    db.execute("PRAGMA query_only=ON")
    return db


def best_levels(production, ticker, cutoff):
    """Best YES/NO bid price and size of the last book retrieved at or before C."""
    row = production.execute(
        "SELECT o.data_json, f.retrieved_at FROM observations o JOIN fetches f ON f.id=o.fetch_id"
        " WHERE o.ticker=? AND o.kind='book' AND f.state='ok' AND f.retrieved_at<=?"
        " ORDER BY f.retrieved_at DESC LIMIT 1",
        (ticker, cutoff),
    ).fetchone()
    if row is None:
        return None
    bids = json.loads(row[0]).get("bids") or {}
    out = {"retrieved_at": row[1]}
    for side in ("yes", "no"):
        levels = [lv for lv in (bids.get(side + "_dollars") or []) if float(lv[1]) > 0]
        best = max(levels, key=lambda lv: float(lv[0])) if levels else None
        out[side] = None if best is None else {"price": best[0], "size": best[1]}
    return out


def compare(candle_rows, book_rows):
    candles = {r["opportunity_key"]: r for r in candle_rows}
    books = {r["opportunity_key"]: r for r in book_rows}
    games, diffs = [], {f: [] for f in FEATURES}
    for key in sorted(set(candles) & set(books)):
        c, b = candles[key], books[key]
        entry = {
            "opportunity_key": key,
            "official_date": c["official_date"],
            "ticker": c["ticker"],
            "cutoff": c["cutoff"],
            "start_corroborated": c["start_corroborated"],
            "last_book_at": b["inputs"]["last_selected_quote_at"],
            "book_quotes_before_c": b["inputs"]["selected_quote_count"],
        }
        for feature in FEATURES:
            cv, bv = c["features"].get(feature), b["features"].get(feature)
            entry[feature] = {"candle": cv, "book": bv}
            if cv is not None and bv is not None:
                entry[feature]["candle_minus_book"] = round(cv - bv, 6)
                diffs[feature].append(cv - bv)
        games.append(entry)
    summary = {}
    for feature, values in diffs.items():
        if values:
            summary[feature] = {
                "n": len(values),
                "mean_candle_minus_book": round(statistics.fmean(values), 6),
                "mean_abs": round(statistics.fmean(abs(v) for v in values), 6),
                "max_abs": round(max(abs(v) for v in values), 6),
                "median": round(statistics.median(values), 6),
            }
        else:
            summary[feature] = {"n": 0}
    return games, summary, sorted(set(candles) - set(books)), sorted(set(books) - set(candles))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--production", required=True)
    p.add_argument("--history-db", required=True)
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--through", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    start, through = date.fromisoformat(args.start), date.fromisoformat(args.through)
    history_db, production = readonly(args.history_db), readonly(args.production)
    try:
        candle_manifest, candle_rows = dataset_history.build(
            history_db,
            feature_set="fs-market-v1",
            start_date=start,
            through_date=through,
            name="check-candles",
            identity_rules=RULES,
        )
        book_manifest, book_rows = collection.build(
            production,
            history_db,
            feature_set="fs-market-v1",
            quote_source="book",
            start_date=start,
            through_date=through,
            name="check-books",
            identity_rules=RULES,
            lineage="live",
        )
    finally:
        history_db.close()
        production.close()
    games, summary, candle_only, book_only = compare(candle_rows, book_rows)
    production = readonly(args.production)
    try:
        sizes = []
        for game in games:
            game["book_best_levels_at_c"] = best_levels(production, game["ticker"], game["cutoff"])
            levels = game["book_best_levels_at_c"] or {}
            for side in ("yes", "no"):
                if levels.get(side):
                    sizes.append(float(levels[side]["size"]))
    finally:
        production.close()
    summary["book_best_level_size"] = (
        {
            "n": len(sizes),
            "min": min(sizes),
            "median": statistics.median(sizes),
            "max": max(sizes),
        }
        if sizes
        else {"n": 0}
    )
    report = {
        "kind": "candle-vs-collected-book-check",
        "period": {"from": args.start, "through": args.through},
        "identity_rules": list(RULES),
        "candle_rows": len(candle_rows),
        "book_rows": len(book_rows),
        "games_in_both": len(games),
        "candle_only": candle_only,
        "book_only": book_only,
        "candle_exclusions": candle_manifest["exclusions"],
        "book_exclusions": book_manifest["exclusions"],
        "summary": summary,
        "games": games,
        "notes": [
            "candle features: closing best bid/ask of one-minute candles ending at or before C",
            "book features: order books retrieved by the production collector at or before C",
            "date-only postseason identities carry start_corroborated=false",
        ],
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {k: report[k] for k in ("candle_rows", "book_rows", "games_in_both", "summary")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
