"""Synthetic offline evidence. The autouse network guard remains active."""

import hashlib
import json
import subprocess
import sys
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


@pytest.mark.parametrize("corrected", [True, False])
def test_inspection_resumed_target_uses_observation_order(
    runner, store, monkeypatch, evidence, corrected
):
    service, _ = runner
    original = service.reader.get

    def interrupt(run_id, stage, *args, **kwargs):
        if stage == "settlement-market":
            raise KeyboardInterrupt
        return original(run_id, stage, *args, **kwargs)

    monkeypatch.setattr(service.reader, "get", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.run(tickers=[TICKER])
    a = store.db.execute("SELECT * FROM settlement_targets").fetchone()
    assert a["market_fetch_id"] is None
    monkeypatch.setattr(service.reader, "get", original)
    service.run(tickers=[TICKER])  # B finishes before A retrieves its market.
    if corrected:
        evidence["market"].update(result="no", settlement_value_dollars="0.0000")
    service.run(tickers=[TICKER], resume=a["run_id"])
    resumed = store.db.execute("SELECT * FROM settlement_targets WHERE id=?", (a["id"],)).fetchone()
    record = inspect(store)["records"][0]
    raw = json.loads(journal.body(store.db, record["latest_evidence"]["market_fetch_id"]))
    print({"latest_result": record["data"]["result"], "evidence_result": raw["market"]["result"]})
    assert raw["market"]["result"] == record["data"]["result"]
    assert record["latest_evidence"]["market_fetch_id"] == resumed["market_fetch_id"]
    assert record["first_evidence"]["market_fetch_id"] == record["first_fetch_id"]
    assert (record["first_fetch_id"] == resumed["market_fetch_id"]) is corrected
    before = counts(store)
    for _ in range(2):
        assert replay(store)["status"] == "complete"
        assert inspect(store)["records"][0] == record
        assert counts(store) == before


def test_inspection_a_b_a_history_and_repeated_observation(runner, store, evidence):
    service, _ = runner
    for result, payout in [
        ("yes", "1.0000"),
        ("no", "0.0000"),
        ("yes", "1.0000"),
        ("yes", "1.0000"),
    ]:
        evidence["market"].update(result=result, settlement_value_dollars=payout)
        service.run(tickers=[TICKER])
    records = inspect(store, history=True)["records"]
    assert [r["data"]["result"] for r in records] == ["yes", "no", "yes"]
    for record in records:
        for name in ("first_evidence", "latest_evidence"):
            proof = record[name]
            market = json.loads(journal.body(store.db, proof["market_fetch_id"]))["market"]
            assert market["result"] == record["data"]["result"]
            assert proof["version_id"] == record["id"]
    assert records[0]["first_fetch_id"] != records[0]["latest_evidence"]["market_fetch_id"]
    assert records[0]["first_fetch_id"] != records[2]["first_fetch_id"]
    before = counts(store)
    for _ in range(2):
        replay(store)
        assert inspect(store, history=True)["records"] == records
        assert counts(store) == before


def test_inspection_separates_incomplete_result_and_mapping_evidence(
    runner, store, evidence, monkeypatch
):
    service, _ = runner
    service.run(tickers=[TICKER])
    old = inspect(store)["records"][0]
    evidence["market"].update(result="no", settlement_value_dollars="0.0000")
    original = service.reader.get

    def interrupt(run_id, stage, *args, **kwargs):
        if stage == "settlement-event":
            raise KeyboardInterrupt
        return original(run_id, stage, *args, **kwargs)

    monkeypatch.setattr(service.reader, "get", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.run(tickers=[TICKER])
    record = inspect(store)["records"][0]
    proof = record["latest_evidence"]
    assert record["data"]["result"] == "no"
    assert proof["status"] == "available" and proof["target_status"] == "incomplete"
    assert proof["event_fetch_id"] is None and proof["schedule_fetch_id"] is None
    assert record["latest_mapping"] == old["latest_mapping"]
    assert record["latest_mlb_result"] == old["latest_mlb_result"]
    mapping_proof = record["latest_mapping"]["latest_evidence"]
    mlb_proof = record["latest_mlb_result"]["latest_evidence"]
    assert mapping_proof["run_id"] == mlb_proof["run_id"] != proof["run_id"]
    assert mapping_proof["market_fetch_id"] == old["latest_evidence"]["market_fetch_id"]
    assert mlb_proof["schedule_fetch_id"] == mlb_proof["observation"]["fetch_id"]
    # Inspection must not fill missing refresh fields from that independent mapping.
    assert proof["mapping_version_id"] is None


def test_inspection_missing_associations_are_explicit(runner, store):
    runner[0].run(tickers=[TICKER])
    original = inspect(store)["records"][0]
    # Simulate an incomplete imported association in this disposable test archive.
    with store.transaction():
        store.db.execute("DELETE FROM settlement_targets")
    record = inspect(store)["records"][0]
    assert record["latest_evidence"]["status"] == "available"
    assert record["latest_evidence"]["target_reason"] == "target_missing"
    assert record["latest_evidence"]["market_fetch_id"] == original["first_fetch_id"]
    assert record["latest_mapping"]["latest_evidence"]["target_status"] == "missing"
    with store.transaction():
        store.db.execute("DELETE FROM settlement_observations")
    record = inspect(store)["records"][0]
    assert record["first_evidence"]["reason"] == "observation_missing"
    assert record["latest_evidence"]["market_fetch_id"] is None
    assert record["latest_mlb_result"]["latest_evidence"]["status"] == "missing"


def test_inspection_readonly_cli_without_mapping(runner, store, monkeypatch):
    service, _ = runner
    original = service.reader.get

    def interrupt(run_id, stage, *args, **kwargs):
        if stage == "settlement-event":
            raise KeyboardInterrupt
        return original(run_id, stage, *args, **kwargs)

    monkeypatch.setattr(service.reader, "get", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.run(tickers=[TICKER])
    before = counts(store)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vader_intelligence.cli",
            "--db",
            str(store.path),
            "settlement",
            "inspect",
            "--ticker",
            TICKER,
            "--history",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["evidence_version"] == 2
    record = output["records"][0]
    assert record["mapping_status"] == record["mlb_status"] == "missing"
    assert record["latest_mapping"] is record["latest_mlb_result"] is None
    assert record["latest_evidence"]["target_status"] == "incomplete"
    assert counts(store) == before
    # Even a present target must not supply context if its link no longer matches.
    with store.transaction():
        store.db.execute("UPDATE settlement_targets SET market_fetch_id=NULL")
    record = inspect(store)["records"][0]
    assert record["latest_evidence"]["target_reason"] == "target_association_missing"
    assert record["latest_evidence"]["market_fetch_id"] == record["first_fetch_id"]
    assert record["latest_evidence"]["terms_fetch_id"] is None


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


def test_verified_doubleheader_rule_and_number_conflict(evidence):
    evidence["market"]["rules_primary"] = evidence["market"]["rules_primary"].replace(
        "game originally", "game 1 of the double header originally"
    )
    assert mapping(evidence)["game_id"] == 123
    evidence["market"]["event_ticker"] += "G2"
    evidence["event"]["event_ticker"] += "G2"
    assert mapping(evidence)["reason"] == "conflicting_game_number"


def test_repeated_schedule_rows_and_conflicting_game_quarantine(runner, store, evidence):
    game = evidence["schedule"]["dates"][0]["games"][0]
    evidence["schedule"]["dates"].append({"games": [deepcopy(game)]})
    assert runner[0].run(tickers=[TICKER])["mapping_states"] == {"mapped": 1}
    evidence["schedule"]["dates"][1]["games"][0]["gameDate"] = "2026-09-27T23:15:00Z"
    assert runner[0].run(tickers=[TICKER])["mapping_states"] == {"quarantined": 1}
    value = inspect(store)["records"][0]
    assert value["latest_mapping"]["reason"] == "conflicting_schedule_entries"
    mlb = store.db.execute(
        "SELECT data_json FROM settlement_versions WHERE kind='mlb' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    import json

    assert json.loads(mlb)["lifecycle"] == "ambiguous"


def test_unknown_rule_quarantined_atomically_and_replayed(runner, store, evidence):
    evidence["market"]["rules_primary"] = "If a first inning run is scored..."
    assert runner[0].run(tickers=[TICKER])["mapping_states"] == {"quarantined": 1}
    assert counts(store)["fetches"] == 4
    assert replay(store)["status"] == "complete"


def test_empty_discovery_is_inconclusive(runner, store, monkeypatch):
    service, _ = runner
    old = service.reader.client._transport.handler

    def handler(request):
        return (
            response(data={"markets": [], "cursor": ""})
            if request.url.path.endswith("/markets")
            else old(request)
        )

    monkeypatch.setattr(service.reader.client._transport, "handler", handler)
    assert service.run()["status"] == "inconclusive"


def test_discovery_cursor_loop_and_cap_are_visible(runner, store, monkeypatch):
    service, _ = runner
    old = service.reader.client._transport.handler

    def handler(request):
        return (
            response(data={"markets": [], "cursor": "repeat"})
            if request.url.path.endswith("/markets")
            else old(request)
        )

    monkeypatch.setattr(service.reader.client._transport, "handler", handler)
    result = service.run(max_pages=3)
    assert result["status"] == "partial" and "cursor loop" in str(result["errors"])
    result = service.run(max_pages=1)
    assert result["status"] == "partial" and result["selection_truncated"]


def test_migration_rolls_back_on_ddl_failure(store):
    store.db.execute("CREATE TABLE settlement_targets (existing TEXT)")
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        migrate(store)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert not store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='settlement_versions'"
    ).fetchone()
    assert store.db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1


def test_interruption_inside_materialization_rolls_back_entire_fetch(runner, store, monkeypatch):
    service, _ = runner
    original = journal.map_target

    def crash(db, key):
        raise KeyboardInterrupt

    monkeypatch.setattr(journal, "map_target", crash)
    with pytest.raises(KeyboardInterrupt):
        service.run(tickers=[TICKER])
    row = store.db.execute("SELECT * FROM fetches ORDER BY seq DESC LIMIT 1").fetchone()
    assert row["state"] == "pending" and row["body_sha256"] is None
    assert (
        store.db.execute("SELECT COUNT(*) FROM settlement_versions WHERE kind='mlb'").fetchone()[0]
        == 0
    )
    run = store.db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    monkeypatch.setattr(journal, "map_target", original)
    assert service.run(tickers=[TICKER], resume=run)["status"] == "complete"
    assert (
        store.db.execute("SELECT state FROM fetches WHERE id=?", (row["id"],)).fetchone()[0]
        == "interrupted"
    )
    assert not store.verify_integrity()


def test_ordered_rebuild_from_archived_evidence(runner, store, evidence):
    service, _ = runner
    service.run(tickers=[TICKER])
    evidence["market"].update(result="no", settlement_value_dollars="0.0000")
    service.run(tickers=[TICKER])
    original = [
        tuple(r)
        for r in store.db.execute(
            "SELECT kind,entity_key,revision,digest,change_kind FROM settlement_versions ORDER BY id"
        )
    ]
    # Explicitly destroy DERIVED state in this synthetic, isolated test database.
    with store.transaction():
        store.db.execute("UPDATE settlement_targets SET mapping_version_id=NULL,state='pending'")
        store.db.execute("DELETE FROM settlement_observations")
        store.db.execute("UPDATE settlement_versions SET previous_id=NULL")
        store.db.execute("DELETE FROM settlement_versions")
    assert replay(store)["status"] == "complete"
    rebuilt = [
        tuple(r)
        for r in store.db.execute(
            "SELECT kind,entity_key,revision,digest,change_kind FROM settlement_versions ORDER BY id"
        )
    ]
    assert rebuilt == original


def test_collector_health_not_masked_by_settlement(runner, store, run):
    # This fixture normally uses kind='test'; health now allowlists actual collector kinds.
    store.db.execute("UPDATE runs SET kind='collect' WHERE id=?", (run,))
    store.finish_run(run, "partial", {"status": "partial"})
    runner[0].run(tickers=[TICKER])
    assert store.health()["latest_run"]["id"] == run


def test_baseline_archived_books_survive_migration_and_replay(store, config):
    from conftest import FIXTURE

    from vader_intelligence.replay import import_fixture

    import_fixture(store, FIXTURE, config)
    tables = ("blobs", "fetches", "entities", "observations", "eligibility")
    before = {t: [tuple(r) for r in store.db.execute(f"SELECT * FROM {t}")] for t in tables}
    migrate(store)
    assert replay(store)["status"] == "complete"
    assert {t: [tuple(r) for r in store.db.execute(f"SELECT * FROM {t}")] for t in tables} == before
    assert not store.verify_integrity()
