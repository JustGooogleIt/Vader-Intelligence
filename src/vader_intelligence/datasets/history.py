"""Point-in-time dataset from a historical-acquisition archive (roadmap B2).

One row per identified game: the selected contract (YES of the lower MLB team id, the
forecast protocol's selection rule) at C = official start − 3600 s, features computed
from quote series cut at C and from team results of earlier official dates, and the
binary label from the selected market's settled result. Rows record the exact input
retrievals (candle and schedule fetch ids) and the label's availability time.

Evidence kind `historical_retrieval`: everything was retrieved after the fact. The
manifest lists the interpretation assumptions; nothing here may be presented as forward
or witnessed evidence.
"""

from datetime import date, datetime, timedelta, timezone

from ..features import definitions, quotes
from ..forecast.references import digest
from ..history import interpret
from ..provenance import json_text, source_hash

DATASET_FORMAT = "dataset-v1"
HORIZON = timedelta(seconds=3600)
FINAL_STATES = {"Final", "Game Over", "Completed Early"}
REGULAR_AND_POST = {"R", "F", "D", "L", "W"}
ASSUMPTIONS = [
    "historical_retrieval: retrieved after the fact; never forward or witnessed evidence",
    "candle values treated as known at end_period_ts",
    "official start from the schedule as retrieved later; contracts whose stated or "
    "ticker start differs from it are excluded, not reinterpreted",
    "team features use results from strictly earlier official dates of the same season",
    "selected contract: YES of the lower MLB team id (forecast protocol selection rule)",
]


def archive_identity(db):
    row = db.execute(
        "SELECT id, seq, body_sha256, retrieved_at FROM fetches ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return {
        "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
        "fetch_ceiling": db.execute("SELECT COALESCE(MAX(seq),0) FROM fetches").fetchone()[0],
        "fetches": db.execute("SELECT COUNT(*) FROM fetches").fetchone()[0],
        "acquisition_runs": [
            r[0]
            for r in db.execute(
                "SELECT id FROM runs WHERE kind='historical-acquire' ORDER BY started_at, id"
            )
        ],
        "latest_fetch_digest": None if row is None else digest(list(row)),
    }


def team_histories(games):
    """team id → time-ordered TeamGame results of final regular/postseason games."""
    histories = {}
    for game in games.values():
        status = (game.get("status") or {}).get("detailedState")
        if status not in FINAL_STATES or game.get("gameType") not in REGULAR_AND_POST:
            continue
        teams = game.get("teams", {})
        away, home = teams.get("away", {}), teams.get("home", {})
        scores = (away.get("score"), home.get("score"))
        if any(type(s) is not int for s in scores) or scores[0] == scores[1]:
            continue
        for side, other in ((away, home), (home, away)):
            team = side.get("team", {}).get("id")
            if type(team) is not int:
                continue
            histories.setdefault(team, []).append(
                (
                    game.get("officialDate"),
                    game.get("gameDate") or "",
                    definitions.TeamGame(
                        official_date=game.get("officialDate"),
                        won=side["score"] > other["score"],
                        runs_for=side["score"],
                        runs_against=other["score"],
                        game_type=game.get("gameType"),
                    ),
                )
            )
    return {team: tuple(g for _, _, g in sorted(entries)) for team, entries in histories.items()}


def before(history, official_date):
    return tuple(g for g in history if g.official_date < official_date)


def label_of(market):
    """(y, reason): 1/0 from a finalized yes/no result with the matching payout."""
    result, value = market.get("result"), market.get("settlement_value")
    if market.get("status") not in ("finalized", "settled") or result not in ("yes", "no"):
        return None, "label_not_final"
    expected = "1.0000" if result == "yes" else "0.0000"
    if value is not None and str(value) not in (expected, expected.rstrip("0").rstrip(".")):
        return None, "label_payout_conflict"
    return (1 if result == "yes" else 0), None


def build(
    db, *, feature_set, start_date, through_date, name, run_id=None, identity_rules=("default",)
):
    """(manifest, rows) for identified games with official dates in [start, through]."""
    if not isinstance(start_date, date) or not isinstance(through_date, date):
        raise ValueError("dates required")
    if through_date < start_date:
        raise ValueError("empty date range")
    definitions.feature_set(feature_set)  # validates the name
    records = interpret.markets(db, run_id)
    games = interpret.schedule(db, run_id)
    histories = team_histories(games)
    exclusions, rows = {}, []

    def exclude(reason):
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for event_ticker, event_markets in sorted(interpret.events(records).items()):
        named = interpret.event_date(event_ticker)
        if named is None:
            exclude("event_ticker_unparsed")
            continue
        if not start_date <= named <= through_date:
            continue
        identity = interpret.identify(event_markets, games, tuple(identity_rules))
        if identity["status"] != "identified":
            exclude("identity:" + identity["reason"])
            continue
        day = identity["official_date"]
        selected = records[identity["selected_ticker"]]
        other = next(m for m in event_markets if m["ticker"] != selected["ticker"])
        y, reason = label_of(selected)
        if y is None:
            exclude(reason)
            continue
        start = datetime.fromisoformat(identity["start"])
        cutoff = start - HORIZON
        selected_candles = interpret.candles(db, selected["ticker"], run_id)
        other_candles = interpret.candles(db, other["ticker"], run_id)
        selected_series = quotes.from_candles(selected["ticker"], selected_candles).until(cutoff)
        other_series = quotes.from_candles(other["ticker"], other_candles).until(cutoff)
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
        features = definitions.compute(feature_set, context)
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
                "features": features,
                "y": y,
                "label_available_at": selected.get("settlement_ts"),
                "inputs": {
                    "selected_candle_fetches": sorted({q.ref for q in selected_series.quotes}),
                    "other_candle_fetches": sorted({q.ref for q in other_series.quotes}),
                    "schedule_fetch": identity["schedule_fetch_id"],
                    "market_fetches": sorted({selected["fetch_id"], other["fetch_id"]}),
                },
            }
        )
    rows.sort(key=lambda r: (r["cutoff"], r["opportunity_key"]))
    data = "".join(json_text(row) + "\n" for row in rows).encode()
    missing = {
        d.name: sum(1 for r in rows if r["features"][d.name] is None)
        for d in definitions.feature_set(feature_set)
    }
    import hashlib

    manifest = {
        "format": DATASET_FORMAT,
        "name": name,
        "evidence_kind": "historical_retrieval",
        "source": {
            "kind": "history_archive",
            "interpretation": interpret.INTERPRETATION,
            "identity_rules": list(identity_rules),
            "archive": archive_identity(db),
            "run_id": run_id,
        },
        "feature_set": definitions.describe_set(feature_set),
        "cutoff_rule": "official start - 3600 s",
        "label_rule": "selected market finalized result yes=1 / no=0 with matching payout",
        "group_rule": "one row per MLB gamePk",
        "period": {"start_date": start_date.isoformat(), "through_date": through_date.isoformat()},
        "rows": len(rows),
        "positives": sum(r["y"] for r in rows),
        "start_uncorroborated_rows": sum(1 for r in rows if not r["start_corroborated"]),
        "exclusions": dict(sorted(exclusions.items())),
        "missing_by_feature": missing,
        "rows_sha256": hashlib.sha256(data).hexdigest(),
        "code_hash": source_hash(),
        "assumptions": ASSUMPTIONS,
    }
    return manifest, rows
