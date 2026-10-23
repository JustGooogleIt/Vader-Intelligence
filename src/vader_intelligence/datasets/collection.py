"""Point-in-time dataset from a collection archive's own retrievals (roadmap B2, H).

Quotes come from what the collector actually retrieved before C (market listings every
cycle, and order books where the game was eligible), using only rows whose retrieval
time is at or before C. Game identity, the official start and labels come from a
historical-acquisition archive (both archives are opened read-only by the caller).

Evidence kind `archived_collection`: contemporaneous retrievals, but never witnessed by
the forecast journal, so this is neither forward evidence nor a forecast record; it is a
research dataset. Comparing it with the candle-based dataset for the same games tests the
history assumption that candle values were known at their end time.
"""

import json
from datetime import date, datetime, timezone

from ..features import definitions, quotes
from ..history import interpret
from ..provenance import json_text, source_hash
from ..storage import retrieval_instant, retrieval_text_bound
from .history import ASSUMPTIONS as HISTORY_ASSUMPTIONS
from .history import HORIZON, archive_identity, before, label_of, team_histories

DATASET_FORMAT = "dataset-v1"
QUOTE_SOURCES = ("listing", "book")
ASSUMPTIONS = [
    "archived_collection: retrieved by the collector before C; not witnessed, not forward",
    "identity, official start and labels from the historical-acquisition archive",
] + [a for a in HISTORY_ASSUMPTIONS if a.startswith("team") or a.startswith("selected")]


def lineage_runs(db, lineage):
    """Run ids of the collection archive whose durable source lineage is `lineage`."""
    from ..forecast.references import run_source_mode

    if lineage not in ("live", "synthetic"):
        raise ValueError("lineage must be live or synthetic")
    return {
        run_id
        for (run_id,) in db.execute("SELECT id FROM runs")
        if run_source_mode(db, run_id) == lineage
    }


def _observations(db, ticker, kind, cutoff, runs):
    """[(retrieved_at, data, fetch_id)] of one ticker and kind retrieved at or before C by
    runs of the selected lineage only. Retrieval times are compared as instants (review
    F23): the SQL text bound is only a prefilter, since stamps may carry any UTC offset."""
    rows = db.execute(
        "SELECT f.retrieved_at, o.data_json, f.id, q.run_id FROM observations o JOIN fetches f"
        " ON f.id=o.fetch_id JOIN requests q ON q.id=f.request_id"
        " WHERE o.ticker=? AND o.kind=? AND f.state='ok' AND f.retrieved_at<=? ORDER BY f.seq",
        (ticker, kind, retrieval_text_bound(cutoff, "upper")),
    ).fetchall()
    out = []
    for at, data, fid, run_id in rows:
        moment = retrieval_instant(at)
        if run_id in runs and moment is not None and moment <= cutoff:
            out.append((moment, json.loads(data), fid))
    return out


def build(
    collection_db,
    history_db,
    *,
    feature_set,
    quote_source,
    start_date,
    through_date,
    name,
    identity_rules=("default",),
    lineage="live",
):
    """(manifest, rows). `lineage` selects collector runs by durable source lineage: a
    real-data dataset uses only live runs; a synthetic one is labelled synthetic."""
    if quote_source not in QUOTE_SOURCES:
        raise ValueError("quote_source must be listing or book")
    if not isinstance(start_date, date) or not isinstance(through_date, date):
        raise ValueError("dates required")
    definitions.feature_set(feature_set)
    records = interpret.markets(history_db)
    games = interpret.schedule(history_db)
    histories = team_histories(games)
    kind = "market" if quote_source == "listing" else "book"
    runs = lineage_runs(collection_db, lineage)
    adapter = quotes.from_listing if quote_source == "listing" else quotes.from_books
    exclusions, rows = {}, []

    def exclude(reason):
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for event_ticker, event_markets in sorted(interpret.events(records).items()):
        named = interpret.event_date(event_ticker)
        if named is None or not start_date <= named <= through_date:
            continue
        identity = interpret.identify(event_markets, games, tuple(identity_rules))
        if identity["status"] != "identified":
            exclude("identity:" + identity["reason"])
            continue
        selected = records[identity["selected_ticker"]]
        other = next(m for m in event_markets if m["ticker"] != selected["ticker"])
        y, reason = label_of(selected)
        if y is None:
            exclude(reason)
            continue
        start = datetime.fromisoformat(identity["start"])
        cutoff = start - HORIZON
        own = _observations(collection_db, selected["ticker"], kind, cutoff, runs)
        if not own:
            exclude(f"no_{quote_source}_before_cutoff")
            continue
        selected_series = adapter(selected["ticker"], own)
        other_series = adapter(
            other["ticker"], _observations(collection_db, other["ticker"], kind, cutoff, runs)
        )
        day = identity["official_date"]
        game = games[identity["game_id"]]
        opponent = identity["away_id"] + identity["home_id"] - identity["selected_team_id"]
        context = definitions.FeatureContext(
            cutoff=cutoff,
            start=start,
            official_date=day,
            game_type=game.get("gameType") or "",
            selected_team=identity["selected_team_id"],
            opponent_team=opponent,
            selected_is_home=identity["selected_team_id"] == identity["home_id"],
            selected=selected_series,
            other=other_series,
            selected_history=before(histories.get(identity["selected_team_id"], ()), day),
            opponent_history=before(histories.get(opponent, ()), day),
        )
        rows.append(
            {
                "opportunity_key": f"game:{identity['game_id']}",
                "group": identity["game_id"],
                "official_date": day,
                "start": start.astimezone(timezone.utc).isoformat(),
                "cutoff": cutoff.astimezone(timezone.utc).isoformat(),
                "ticker": selected["ticker"],
                "event_ticker": event_ticker,
                "selected_team_id": identity["selected_team_id"],
                "rule_version": identity["rule_version"],
                "start_corroborated": identity["start_corroborated"],
                "features": definitions.compute(feature_set, context),
                "y": y,
                "label_available_at": selected.get("settlement_ts"),
                "inputs": {
                    "selected_quote_fetches": sorted({q.ref for q in selected_series.quotes})[-50:],
                    "selected_quote_count": len(selected_series.quotes),
                    "last_selected_quote_at": selected_series.quotes[-1].at.isoformat(),
                    "schedule_fetch": identity["schedule_fetch_id"],
                    "label_fetch": selected["fetch_id"],
                },
            }
        )
    rows.sort(key=lambda r: (r["cutoff"], r["opportunity_key"]))
    import hashlib

    data = "".join(json_text(row) + "\n" for row in rows).encode()
    manifest = {
        "format": DATASET_FORMAT,
        "name": name,
        "evidence_kind": "archived_collection" if lineage == "live" else "synthetic",
        "source": {
            "kind": "collection_archive",
            "quote_source": quote_source,
            "lineage": lineage,
            "collection_archive": _collection_identity(collection_db),
            "label_and_identity_archive": archive_identity(history_db),
            "interpretation": interpret.INTERPRETATION,
            "identity_rules": list(identity_rules),
        },
        "feature_set": definitions.describe_set(feature_set),
        "cutoff_rule": "official start - 3600 s; quotes retrieved at or before C",
        "label_rule": "selected market finalized result yes=1 / no=0 with matching payout",
        "group_rule": "one row per MLB gamePk",
        "period": {"start_date": start_date.isoformat(), "through_date": through_date.isoformat()},
        "rows": len(rows),
        "positives": sum(r["y"] for r in rows),
        "start_uncorroborated_rows": sum(1 for r in rows if not r["start_corroborated"]),
        "exclusions": dict(sorted(exclusions.items())),
        "missing_by_feature": {
            d.name: sum(1 for r in rows if r["features"][d.name] is None)
            for d in definitions.feature_set(feature_set)
        },
        "rows_sha256": hashlib.sha256(data).hexdigest(),
        "code_hash": source_hash(),
        "assumptions": ASSUMPTIONS,
    }
    return manifest, rows


def _collection_identity(db):
    row = db.execute(
        "SELECT id, seq, body_sha256, retrieved_at FROM fetches ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    from ..forecast.references import digest

    return {
        "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
        "fetch_ceiling": db.execute("SELECT COALESCE(MAX(seq),0) FROM fetches").fetchone()[0],
        "latest_fetch_digest": None if row is None else digest(list(row)),
    }
