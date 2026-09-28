"""Synthetic offline evidence. The autouse network guard remains active."""

import hashlib
from copy import deepcopy

import pytest
from conftest import response
from test_transport import reader_for

from vader_intelligence.mapping import resolve
from vader_intelligence.replay import replay
from vader_intelligence.settlement import journal
from vader_intelligence.settlement.models import kalshi_result, mlb_result
from vader_intelligence.settlement.schema import migrate, require_schema
from vader_intelligence.settlement.service import SettlementRefresh, inspect
from vader_intelligence.storage import Store

TICKER = "KXMLBGAME-26SEP261915CHCBOS-CHC"
EVENT = "KXMLBGAME-26SEP261915CHCBOS"
PDF = b"%PDF-1.4 synthetic contract fixture"


@pytest.fixture
def evidence():
    market = {
        "ticker": TICKER,
        "event_ticker": EVENT,
        "market_type": "binary",
        "yes_sub_title": "Chicago C",
        "rules_primary": "If Chicago C wins the Chicago C vs Boston professional baseball game originally scheduled for Sep 26, 2026 at 7:15 PM EDT, then the market resolves to Yes.",
        "notional_value_dollars": "1.0000",
        "status": "finalized",
        "result": "yes",
        "settlement_value_dollars": "1.0000",
        "settlement_ts": "2026-09-27T03:00:00Z",
        "updated_time": "2026-09-27T03:00:01Z",
    }
    event = {
        "event_ticker": EVENT,
        "series_ticker": "KXMLBGAME",
        "product_metadata": {"competition": "Pro Baseball", "competition_scope": "Game"},
    }
    game = {
        "gamePk": 123,
        "gameDate": "2026-09-26T23:15:00Z",
        "officialDate": "2026-09-26",
        "gameNumber": 1,
        "gameType": "R",
        "doubleHeader": "N",
        "isTie": False,
        "status": {"abstractGameState": "Final", "detailedState": "Final", "startTimeTBD": False},
        "teams": {
            "away": {"team": {"id": 112}, "score": 3, "isWinner": True},
            "home": {"team": {"id": 111}, "score": 1, "isWinner": False},
        },
    }
    return {"market": market, "event": event, "schedule": {"dates": [{"games": [game]}]}}


@pytest.fixture
def runner(store, config, monkeypatch, evidence):
    migrate(store)
    monkeypatch.setattr(journal, "REVIEWED_TERMS", {hashlib.sha256(PDF).hexdigest()})
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path.endswith("/series/KXMLBGAME"):
            return response(
                data={"series": {"ticker": "KXMLBGAME", "contract_terms_url": journal.TERMS_URL}}
            )
        if path.endswith(".pdf"):
            return response(body=PDF)
        if path.endswith("/markets/" + TICKER):
            return response(data={"market": evidence["market"]})
        if path.endswith("/events/" + EVENT):
            return response(data={"event": evidence["event"]})
        if path.endswith("/schedule"):
            return response(data=evidence["schedule"])
        if path.endswith("/markets"):
            return response(data={"markets": [evidence["market"]], "cursor": ""})
        raise AssertionError(request.url)

    reader = reader_for(store, config, handler)
    yield SettlementRefresh(store, reader, config), calls
    reader.close()


def counts(store):
    return {
        name: store.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in (
            "fetches",
            "blobs",
            "settlement_versions",
            "settlement_observations",
            "settlement_targets",
        )
    }


def mapping(evidence, **kwargs):
    return resolve(
        evidence["market"], evidence["event"], evidence["schedule"], reviewed_terms=True, **kwargs
    )


def test_explicit_additive_migration_preserves_baseline(store, run, config):
    baseline = [tuple(r) for r in store.db.execute("SELECT * FROM runs")]
    with pytest.raises(ValueError, match="explicit migration"):
        require_schema(store)
    migrate(store)
    migrate(store)
    assert [tuple(r) for r in store.db.execute("SELECT * FROM runs")] == baseline
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
    reopened = Store(config.database, readonly=True)
    reopened.close()
    assert not store.verify_integrity()


@pytest.mark.parametrize("state", ["active", "closed", "determined", "disputed", "amended"])
def test_pending_not_paid_out(evidence, state):
    evidence["market"]["status"] = state
    data = kalshi_result(evidence["market"])
    assert data["lifecycle"] == "pending" and data["binary_outcome"] is None


@pytest.mark.parametrize(
    "result,payout,kind,label",
    [
        ("yes", "1.0000", "binary", "yes"),
        ("no", "0.0000", "binary", "no"),
        ("scalar", "0.5000", "exceptional", None),
        ("scalar", "1.0000", "exceptional", None),
        ("yes", "0.1234", "exceptional", None),
        ("no", "1.0000", "exceptional", None),
    ],
)
def test_exact_payout_categories(evidence, result, payout, kind, label):
    evidence["market"].update(result=result, settlement_value_dollars=payout)
    data = kalshi_result(evidence["market"])
    assert (data["yes_payout"], data["payout_kind"], data["binary_outcome"]) == (
        payout,
        kind,
        label,
    )
    if payout == "0.1234":
        assert data["no_payout"] == "0.8766"


@pytest.mark.parametrize(
    "payout", [None, 0.5, "NaN", "1.01", "-0.1", "1e0", "0.0000000000000000001"]
)
def test_invalid_payout_never_fabricated(evidence, payout):
    evidence["market"]["settlement_value_dollars"] = payout
    data = kalshi_result(evidence["market"])
    assert data["binary_outcome"] is None and data["yes_payout"] is None


def test_missing_timestamp_and_is_provisional_semantics(evidence):
    evidence["market"]["is_provisional"] = True
    assert kalshi_result(evidence["market"])["binary_outcome"] == "yes"
    del evidence["market"]["settlement_ts"]
    result = kalshi_result(evidence["market"])
    assert result["lifecycle"] == "finalized" and result["binary_outcome"] is None
    assert "settlement_ts" not in result
    assert "missing_settlement_ts" in result["flags"]


def test_ordinary_doubleheader_ambiguity_and_reversal(evidence):
    game = evidence["schedule"]["dates"][0]["games"][0]
    assert mapping(evidence)["game_id"] == 123
    game["doubleHeader"] = "Y"
    second = deepcopy(game)
    second.update(gamePk=124, gameNumber=2, gameDate="2026-09-27T00:15:00Z")
    evidence["schedule"]["dates"][0]["games"].append(second)
    assert mapping(evidence)["game_id"] == 123
    second["gameDate"] = game["gameDate"]
    assert mapping(evidence)["reason"] == "ambiguous_game"
    evidence["market"]["event_ticker"] += "G2"
    evidence["event"]["event_ticker"] += "G2"
    assert mapping(evidence)["game_id"] == 124
    second["reverseHomeAwayStatus"] = True
    assert mapping(evidence)["reason"] == "home_away_reversed"


def test_changed_schedule_needs_stable_identity_or_official_link(evidence):
    prior = mapping(evidence)
    game = evidence["schedule"]["dates"][0]["games"][0]
    game["gameDate"] = "2026-09-27T23:15:00Z"
    game["officialDate"] = "2026-09-27"
    assert mapping(evidence)["status"] == "quarantined"
    assert mapping(evidence, prior=prior)["reason"] == "evidenced_schedule_change"
    game["rescheduledFrom"] = "2026-09-26T23:15:00Z"
    assert mapping(evidence)["game_id"] == 123
    # A calendar date alone does not prove doubleheader identity.
    game["rescheduledFrom"] = "2026-09-26"
    assert mapping(evidence)["status"] == "quarantined"


@pytest.mark.parametrize("state", ["Postponed", "Cancelled", "Suspended"])
def test_abnormal_game_does_not_determine_contract(evidence, state):
    game = evidence["schedule"]["dates"][0]["games"][0]
    game["status"]["detailedState"] = state
    assert mapping(evidence)["game_id"] == 123
    assert mlb_result(game)["lifecycle"] == state.lower()
    assert mlb_result(game)["winner_team_id"] is None
    assert kalshi_result(evidence["market"])["binary_outcome"] == "yes"


def test_repeat_refresh_new_retrievals_replay_idempotent(runner, store):
    service, calls = runner
    assert service.run(tickers=[TICKER])["status"] == "complete"
    first = counts(store)
    assert service.run(tickers=[TICKER])["status"] == "complete"
    second = counts(store)
    assert second["fetches"] == 2 * first["fetches"] == 10
    assert second["blobs"] == first["blobs"]
    assert second["settlement_versions"] == first["settlement_versions"] == 3
    assert second["settlement_observations"] == 2 * first["settlement_observations"] == 6
    for _ in range(2):
        assert replay(store)["status"] == "complete"
        assert counts(store) == second
    record = inspect(store)["records"][0]
    assert record["latest_mapping"]["game_id"] == 123
    assert record["latest_mlb_result"]["winner_team_id"] == 112
    assert not store.verify_integrity()


def test_corrections_append_a_b_a_and_official_result_independent(runner, store, evidence):
    service, _ = runner
    service.run(tickers=[TICKER])
    evidence["market"].update(result="no", settlement_value_dollars="0.0000")
    service.run(tickers=[TICKER])
    evidence["market"].update(result="yes", settlement_value_dollars="1.0000")
    service.run(tickers=[TICKER])
    rows = store.db.execute(
        "SELECT * FROM settlement_versions WHERE kind='kalshi' ORDER BY revision"
    ).fetchall()
    assert [r["change_kind"] for r in rows] == ["initial", "corrected", "corrected"]
    assert rows[0]["digest"] == rows[2]["digest"] and rows[2]["previous_id"] == rows[1]["id"]
    game = evidence["schedule"]["dates"][0]["games"][0]
    game["teams"]["away"].update(isWinner=False, score=1)
    game["teams"]["home"].update(isWinner=True, score=4)
    service.run(tickers=[TICKER])
    assert (
        store.db.execute("SELECT COUNT(*) FROM settlement_versions WHERE kind='kalshi'").fetchone()[
            0
        ]
        == 3
    )
    assert (
        store.db.execute(
            "SELECT change_kind FROM settlement_versions WHERE kind='mlb' ORDER BY id DESC"
        ).fetchone()[0]
        == "corrected"
    )
    assert inspect(store)["records"][0]["data"]["binary_outcome"] == "yes"
    assert replay(store)["status"] == "complete"


def test_changed_mapping_versions_and_stable_repeated_evidence(runner, store, evidence):
    service, _ = runner
    service.run(tickers=[TICKER])
    evidence["schedule"]["dates"][0]["games"][0]["gameDate"] = "2026-09-27T23:15:00Z"
    service.run(tickers=[TICKER])
    before = counts(store)["settlement_versions"]
    service.run(tickers=[TICKER])
    assert counts(store)["settlement_versions"] == before
    assert inspect(store)["records"][0]["latest_mapping"]["prior_mapping_version_id"]


def test_unknown_terms_quarantined(runner, store, monkeypatch):
    monkeypatch.setattr(journal, "REVIEWED_TERMS", set())
    assert runner[0].run(tickers=[TICKER])["mapping_states"] == {"quarantined": 1}
    assert inspect(store)["records"][0]["latest_mapping"]["reason"] == "unreviewed_contract_terms"


def test_parse_failure_preserves_raw_atomic_target_progress(runner, store, evidence):
    evidence["schedule"] = {"dates": [{"games": [{"gamePk": 123, "teams": None}]}]}
    result = runner[0].run(tickers=[TICKER])
    assert result["status"] == "partial"
    target = store.db.execute("SELECT * FROM settlement_targets").fetchone()
    assert target["market_fetch_id"] and not target["schedule_fetch_id"]
    assert not target["mapping_version_id"]
    row = store.db.execute("SELECT * FROM fetches ORDER BY seq DESC").fetchone()
    assert row["state"] == "parse_error" and row["body_sha256"]
    evidence["schedule"] = {"dates": []}
    resumed = runner[0].run(tickers=[TICKER], resume=result["run_id"])
    assert resumed["status"] == "complete"
    assert counts(store)["fetches"] == 6


def test_interruption_after_market_commit_resumes_without_duplicates(runner, store, monkeypatch):
    service, calls = runner
    original = service.reader.get

    def stop(run_id, stage, *args, **kwargs):
        if stage == "settlement-event":
            raise KeyboardInterrupt
        return original(run_id, stage, *args, **kwargs)

    monkeypatch.setattr(service.reader, "get", stop)
    with pytest.raises(KeyboardInterrupt):
        service.run(tickers=[TICKER])
    run = store.db.execute("SELECT * FROM runs ORDER BY rowid DESC").fetchone()
    assert run["status"] == "interrupted"
    assert counts(store)["fetches"] == 3
    monkeypatch.setattr(service.reader, "get", original)
    assert service.run(tickers=[TICKER], resume=run["id"])["status"] == "complete"
    assert counts(store)["fetches"] == 5


def test_authorization_failure_aborts_all_targets(runner, store, monkeypatch):
    service, calls = runner
    monkeypatch.setattr(
        service.reader.client,
        "stream",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not used")),
    )
    # A real mock transport response exercises the existing terminal HTTP handling.
    service.reader = reader_for(store, service.config, lambda req: response(403))
    result = service.run(tickers=[TICKER])
    assert result["status"] == "partial" and "403" in str(result["errors"])
    assert counts(store)["fetches"] == 1
    service.reader.close()


def test_discovery_and_empty_universe(runner, store, evidence):
    service, _ = runner
    assert service.run()["status"] == "complete"
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM requests WHERE stage='settlement-discovery'"
        ).fetchone()[0]
        == 1
    )


def test_total_budget_and_invalid_bounds(runner, store):
    service, _ = runner
    result = service.run(tickers=[TICKER], budget=1)
    assert result["status"] == "partial" and counts(store)["fetches"] <= 2
    with pytest.raises(ValueError, match="require limit"):
        service.run(limit=51)
    with pytest.raises(ValueError):
        service.run(budget=float("nan"))
