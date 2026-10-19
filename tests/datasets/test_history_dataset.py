"""Point-in-time dataset, registry and splits (roadmap B1/B2). Synthetic data only.

Builds a history archive with the synthetic acquisition transport of
tests/history/test_acquire.py, then checks the dataset's point-in-time guarantees
directly: quotes after C and same-day results cannot change a row, labels follow the
settled result, builds are deterministic, and the registry/holdout ledger are
append-only and explicit.
"""

import copy
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from history import test_acquire as synthetic

from vader_intelligence.datasets import history as dataset_history
from vader_intelligence.datasets import splits
from vader_intelligence.features import definitions, quotes
from vader_intelligence.history import interpret
from vader_intelligence.research.registry import Registry

DAY = date(2026, 9, 20)


def build(store, config, *, handler=None, feature_set="fs-full-v1", **kwargs):
    synthetic.acquire(store, config, [], **({"handler": handler} if handler else {}))
    return dataset_history.build(
        store.db, feature_set=feature_set, start_date=DAY, through_date=DAY, name="t", **kwargs
    )


def test_rows_labels_inputs_and_manifest(store, config):
    manifest, rows = build(store, config)
    assert manifest["evidence_kind"] == "historical_retrieval"
    assert manifest["rows"] == 2 and len(rows) == 2
    assert manifest["exclusions"] == {"identity:start_differs": 1}
    by_key = {r["opportunity_key"]: r for r in rows}
    phi = by_key["game:900001"]
    assert phi["ticker"].endswith("-NYM") and phi["selected_team_id"] == 121
    assert phi["y"] == 1  # NYM (home) won in the fixture
    assert phi["cutoff"] == "2026-09-20T16:10:00+00:00"
    assert phi["features"]["mid_c"] == pytest.approx(0.605)  # historical variant 0.60/0.61
    assert phi["features"]["selected_is_home"] == 1.0
    assert phi["inputs"]["selected_candle_fetches"] and phi["inputs"]["schedule_fetch"]
    assert phi["start_corroborated"] is True
    assert manifest["feature_set"]["forward_capable"] is False  # team features included
    assert manifest["rows_sha256"] and manifest["code_hash"]


def test_a_quote_after_the_cutoff_cannot_change_a_row(store, config, tmp_path):
    _, baseline = build(store, config)

    original = synthetic.handler_factory

    def with_future(calls, **kwargs):
        inner = original(calls, **kwargs)

        def handler(request):
            result = inner(request)
            if request.url.path.endswith("/candlesticks"):
                data = json.loads(result.read())
                last = data["candlesticks"][-1]
                future = copy.deepcopy(last)
                future["end_period_ts"] = last["end_period_ts"] + 600
                for side in ("yes_bid", "yes_ask"):
                    for k in list(future[side]):
                        future[side][k] = "0.9900"
                data["candlesticks"].append(future)
                return synthetic.response(data=data)
            return result

        return handler

    synthetic.handler_factory = with_future
    try:
        from vader_intelligence.storage import Store

        other = Store(tmp_path / "future.sqlite3", min_free_bytes=1)
        try:
            _, rows = build(other, config)
            archived = interpret.candles(other.db, "KXMLBGAME-26SEP201310PHINYM-NYM")
            cutoff = datetime(2026, 9, 20, 16, 10, tzinfo=timezone.utc)
            assert any(c["end"] > cutoff and c["yes_bid_close"] == "0.9900" for c in archived)
        finally:
            other.close()
    finally:
        synthetic.handler_factory = original
    assert [r["features"] for r in rows] == [r["features"] for r in baseline]


def test_feature_context_refuses_post_cutoff_inputs():
    cutoff = datetime(2026, 9, 20, 16, 10, tzinfo=timezone.utc)
    late = quotes.Quote(cutoff + timedelta(seconds=1), None, None, None, None, None, "f")
    series = quotes.QuoteSeries("T", "candles", "period", (late,))
    with pytest.raises(ValueError, match="after the cutoff"):
        definitions.FeatureContext(
            cutoff=cutoff,
            start=cutoff + timedelta(hours=1),
            official_date="2026-09-20",
            game_type="R",
            selected_team=1,
            opponent_team=2,
            selected_is_home=True,
            selected=series,
            other=None,
        )
    same_day = definitions.TeamGame("2026-09-20", True, 3, 1, "R")
    with pytest.raises(ValueError, match="game date or later"):
        definitions.FeatureContext(
            cutoff=cutoff,
            start=cutoff,
            official_date="2026-09-20",
            game_type="R",
            selected_team=1,
            opponent_team=2,
            selected_is_home=True,
            selected=series.until(cutoff),
            other=None,
            selected_history=(same_day,),
        )


def test_team_history_uses_only_earlier_official_dates():
    games = {
        1: synthetic.game(1, 143, 121, "2026-09-19T17:10:00Z", official="2026-09-19"),
        2: synthetic.game(2, 143, 121, "2026-09-20T17:10:00Z"),  # the day itself
    }
    histories = dataset_history.team_histories(games)
    earlier = dataset_history.before(histories[121], "2026-09-20")
    assert [g.official_date for g in earlier] == ["2026-09-19"]
    assert dataset_history.before(histories[121], "2026-09-19") == ()


def test_builds_are_deterministic_and_the_registry_deduplicates(store, config, tmp_path):
    manifest, rows = build(store, config)
    again, rows_again = dataset_history.build(
        store.db, feature_set="fs-full-v1", start_date=DAY, through_date=DAY, name="t"
    )
    assert again == manifest and rows_again == rows
    registry = Registry(tmp_path / "research")
    try:
        first = registry.register_dataset(manifest, rows)
        assert registry.register_dataset(again, rows_again) == first
        assert registry.dataset_rows(first) == rows
        with pytest.raises(ValueError, match="rows_sha256"):
            registry.register_dataset(manifest | {"name": "tampered"}, rows[:1])
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            registry.db.execute("UPDATE datasets SET name='x'")
    finally:
        registry.close()


def test_market_only_feature_set_is_forward_capable(store, config):
    manifest, rows = build(store, config, feature_set="fs-market-v1")
    assert manifest["feature_set"]["forward_capable"] is True
    assert set(rows[0]["features"]) == {d.name for d in definitions.feature_set("fs-market-v1")}


def test_splits_embargo_groups_and_holdout_isolation():
    rows = [
        {"official_date": f"2026-09-{d:02d}", "group": d, "opportunity_key": f"g{d}"}
        for d in range(1, 31)
    ]
    split = splits.SplitDefinition("2026-09-15", "2026-09-22", embargo_days=1)
    assert splits.counts(rows, split) == {
        "development": 15,
        "validation": 6,
        "embargo": 2,
        "holdout": 7,
    }
    dev, val = splits.development_and_validation(rows, split)
    assert max(r["official_date"] for r in dev) < min(r["official_date"] for r in val)
    holdout = splits.holdout_rows(rows, split)
    assert min(r["official_date"] for r in holdout) == "2026-09-24"
    assert not {r["group"] for r in holdout} & {r["group"] for r in dev + val}
    with pytest.raises(ValueError, match="spans segments"):
        splits.assign(rows + [{"official_date": "2026-09-29", "group": 1}], split)
    with pytest.raises(ValueError):
        splits.SplitDefinition("2026-09-15", "2026-09-16", embargo_days=1)


def test_holdout_ledger_records_consumption_then_reuse(store, config, tmp_path):
    manifest, rows = build(store, config)
    registry = Registry(tmp_path / "research")
    try:
        dataset_id = registry.register_dataset(manifest, rows)
        models = [
            registry.register_model(
                json.dumps({"model": n}), family="logistic", dataset_id=dataset_id, summary={}
            )
            for n in (1, 2)
        ]
        assert registry.holdout_status(dataset_id, "h")["untouched"] is True
        first = registry.evaluate_holdout(models[0], dataset_id, "h", {"log_loss": "0.6"})
        assert first["consumed"] and first["evaluations"] == 1
        second = registry.evaluate_holdout(models[1], dataset_id, "h", {"log_loss": "0.7"})
        assert second["evaluations"] == 2 and not second["untouched"]
        kinds = [r[0] for r in registry.db.execute("SELECT kind FROM holdout_ledger ORDER BY id")]
        assert kinds == ["consumed", "reused"]
        biased = [
            r[0]
            for r in registry.db.execute("SELECT selection_biased FROM evaluations ORDER BY id")
        ]
        assert biased == [0, 1]  # every evaluation after the first is not untouched
        with pytest.raises(ValueError, match="already evaluated"):
            registry.evaluate_holdout(models[0], dataset_id, "h", {"log_loss": "0.5"})
    finally:
        registry.close()


def test_date_only_identity_is_opt_in_and_flagged():
    rule = (
        "If {w} wins the Cincinnati {joiner} Philadelphia professional baseball game"
        " {when}scheduled for Jul 4, 2025, then the market resolves to Yes."
    )

    def records(joiner="vs", when="originally ", event="KXMLBGAME-25JUL04CINPHI"):
        out = []
        for code, team in (("CIN", "Cincinnati"), ("PHI", "Philadelphia")):
            raw = {
                "ticker": f"{event}-{code}",
                "event_ticker": event,
                "market_type": "binary",
                "yes_sub_title": team,
                "rules_primary": rule.format(w=team, joiner=joiner, when=when),
            }
            out.append({"ticker": raw["ticker"], "raw": raw})
        return out

    games = {
        7: synthetic.game(7, 113, 143, "2025-07-04T23:05:00Z", official="2025-07-04")
        | {"_fetch_id": "s"}
    }
    default = interpret.identify(records(), games)
    assert default == {"status": "unresolved", "reason": "ticker_unparsed"}
    rules = ("default", interpret.DATE_ONLY_RULE)
    for joiner, when in (("vs", "originally "), ("at", "originally "), ("vs", "")):
        found = interpret.identify(records(joiner, when), games, rules)
        assert found["status"] == "identified" and found["start_corroborated"] is False
        assert found["start"] == "2025-07-04T23:05:00+00:00"
        assert found["selected_ticker"].endswith("-CIN")  # 113 < 143
    mismatch = interpret.identify(records(event="KXMLBGAME-25JUL05CINPHI"), games, rules)
    assert mismatch["reason"] == "ticker_identity_mismatch"
    doubled = {7: games[7] | {"doubleHeader": "Y"}}
    assert interpret.identify(records(), doubled, rules)["status"] == "unresolved"
    two = games | {8: games[7] | {"gamePk": 8}}
    assert interpret.identify(records(), two, rules)["reason"] == "ambiguous_game"
    with pytest.raises(ValueError):
        interpret.identify(records(), games, ("history-date-only",))
