"""J1/J2 projections over a SYNTHETIC two-game archive: states, source times, identity
pins and isolation between game views. No network; disposable files only."""

import json
import sqlite3
from datetime import datetime, timezone

from vader_intelligence.command_center import projections
from vader_intelligence.command_center.config import CenterConfig
from vader_intelligence.command_center.store import CenterStore

from ._synthetic import EV1, EV2

NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)  # before both cutoffs


def test_every_projection_carries_state_source_time_and_generated_at(cfg):
    for name in ("overview", "games", "farms", "engineering", "archive_summary", "collection"):
        out = getattr(projections, name)(cfg, NOW)
        assert set(out) >= {"state", "source_time", "generated_at"}, name
        assert out["generated_at"] == NOW.isoformat()
        json.dumps(out)  # JSON-safe


def test_games_are_grouped_by_canonical_game_id_with_countdowns(cfg):
    out = projections.games(cfg, NOW)
    assert out["state"] == "ok" and out["label"] == "fixture"
    by_id = {g["game_id"]: g for g in out["games"]}
    assert set(by_id) == {824948, 824949}
    hou = by_id[824948]
    assert hou["event_ticker"] == EV1 and hou["away"]["name"] == "Houston Astros"
    assert hou["cutoff"] == "2026-09-27T19:03:00+00:00"
    assert hou["countdown_seconds"] == 63 * 60 and not hou["post_cutoff"]
    assert hou["identity"] == "canonical" and hou["can_launch"]
    assert [c["ticker"] for c in hou["contracts"]] == [EV1 + "-HOU"]
    nyy = by_id[824949]
    assert nyy["event_ticker"] == EV2 and nyy["home"]["id"] == 111
    assert nyy["countdown_seconds"] == 6 * 3600 + 3 * 60
    assert out["source_time"] == "2026-09-27T15:00:08+00:00"


def test_two_game_views_do_not_blend(cfg):
    a = projections.game(cfg, 824948, NOW)
    b = projections.game(cfg, 824949, NOW)
    assert a["state"] == b["state"] == "ok"
    assert a["pins"]["market_tickers"] == [EV1 + "-HOU"]
    assert b["pins"]["market_tickers"] == [EV2 + "-NYY"]
    assert {o["ticker"] for o in a["observations"]} == {EV1 + "-HOU"}
    assert {o["ticker"] for o in b["observations"]} == {EV2 + "-NYY"}
    assert {e["game_id"] for e in a["eligibility"]} == {824948}
    assert {e["game_id"] for e in b["eligibility"]} == {824949}
    assert a["decisions"] == [] and a["evaluation"]["items"] == []  # empty, labelled
    assert a["settlement"]["available"] and a["settlement"]["versions"] == []
    assert a["runs"] == [] and a["warnings"] == []
    assert a["pins"]["label"] == "fixture"


def test_post_cutoff_is_a_warning_not_a_change_of_evidence(cfg):
    later = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    out = projections.game(cfg, 824948, later)
    assert out["game"]["post_cutoff"] and out["game"]["countdown_seconds"] < 0
    assert any(w.startswith("post_cutoff") for w in out["warnings"])
    before = projections.game(cfg, 824948, NOW)
    assert out["observations"] == before["observations"]  # the evidence is unchanged


def test_ambiguous_identity_disables_launch(cfg):
    db = sqlite3.connect(cfg.archive)
    with db:
        # The NYY event's other market is mapped to the HOU game: one event, two games.
        db.execute(
            "INSERT INTO eligibility(run_id,ticker,checked_at,schedule_fetch_id,market_fetch_id,game_id,"
            "eligible,reason,cutoff,data_json) VALUES ('fixture-two-games',?,?,?,?,?,1,'eligible',?,'{}')",
            (
                EV2 + "-BOS",
                "2026-09-27T15:00:09+00:00",
                "fixture-schedule-5",
                "fixture-event-7",
                824948,
                "2026-09-27T19:03:00+00:00",
            ),
        )
    db.close()
    out = projections.games(cfg, NOW)
    by_id = {g["game_id"]: g for g in out["games"]}
    assert by_id[824948]["identity"] == "ambiguous" and not by_id[824948]["can_launch"]
    assert "event_ticker_ambiguous" in by_id[824948]["identity_reasons"]
    assert "event_maps_to_several_games" in by_id[824949]["identity_reasons"]
    ws = projections.game(cfg, 824948, NOW)
    assert "identity ambiguous: launch disabled" in ws["warnings"]


def test_unmapped_contracts_are_listed_separately(cfg):
    db = sqlite3.connect(cfg.archive)
    with db:
        db.execute(
            "INSERT INTO eligibility(run_id,ticker,checked_at,schedule_fetch_id,market_fetch_id,game_id,"
            "eligible,reason,cutoff,data_json) VALUES ('fixture-two-games','KXMLBGAME-26SEP27XXXX-ZZZ',"
            "'2026-09-27T15:00:10+00:00',NULL,NULL,NULL,0,'identity_unresolved',NULL,'{}')",
        )
    db.close()
    out = projections.games(cfg, NOW)
    assert out["unmapped"] == [
        {
            "ticker": "KXMLBGAME-26SEP27XXXX-ZZZ",
            "reason": "identity_unresolved",
            "checked_at": "2026-09-27T15:00:10+00:00",
        }
    ]
    assert len(out["games"]) == 2


def test_missing_empty_and_locked_archives_are_distinct_states(tmp_path, dev_root, cfg):
    missing = CenterConfig(root=dev_root, archive=tmp_path / "absent.sqlite3")
    assert projections.games(missing, NOW)["state"] == "unavailable"
    assert projections.overview(missing, NOW)["sources"]["archive"] == "unavailable"
    empty = tmp_path / "empty.sqlite3"
    from vader_intelligence.storage import Store

    Store(empty, min_free_bytes=1).close()
    out = projections.archive_summary(CenterConfig(root=dev_root, archive=empty), NOW)
    assert out["state"] == "empty" and out["counts"]["fetches"] == 0
    assert projections.games(CenterConfig(root=dev_root, archive=empty), NOW)["state"] == "empty"
    # A writer holding an exclusive lock makes the archive unavailable within the budget.
    holder = sqlite3.connect(cfg.archive, isolation_level=None)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("CREATE TABLE lock_probe(x)")
    try:
        import time

        started = time.perf_counter()
        out = projections.games(cfg, NOW)
        assert time.perf_counter() - started < 1.0
        assert out["state"] == "unavailable" and "busy" in out["reason"]
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_collection_states_from_ops_history(tmp_path, dev_root, two_game_archive):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ops = tmp_path / "ops.json"
    config = {
        "version": 1,
        "database": str(two_game_archive),
        "state": str(state_dir),
        "cadence": 120,
        "grace": 5,
        "deadline": 90,
        "test_label": True,
        "shadow": {},
    }
    ops.write_text(json.dumps(config))
    cfg = CenterConfig(root=dev_root, archive=two_game_archive, ops_config=ops)
    assert projections.collection(CenterConfig(root=dev_root), NOW)["state"] == "not_configured"
    assert projections.collection(cfg, NOW)["state"] == "unavailable"  # no history yet
    (state_dir / "history.json").write_text(
        json.dumps({"version": 1, "database": str(two_game_archive), "sequence": 0, "history": []})
    )
    out = projections.collection(cfg, NOW)
    assert (
        out["state"] == "empty"
        and out["status"] == "never_invoked"
        and out["shadow"]["status"] == "never_invoked"
    )
    entry = {
        "start_epoch": NOW.timestamp() - 30,
        "started_at": "2026-09-27T17:59:30+00:00",
        "ended_at": "2026-09-27T17:59:50+00:00",
        "status": "collected_eligible_games",
        "tick": {"status": "inconclusive", "decisions_eligible": 0, "decisions_abstained": 0},
    }
    (state_dir / "history.json").write_text(
        json.dumps(
            {"version": 1, "database": str(two_game_archive), "sequence": 1, "history": [entry]}
        )
    )
    out = projections.collection(cfg, NOW)
    assert out["state"] == "ok" and out["shadow"]["status"] == "waiting_for_cutoff"
    assert out["source_time"] == "2026-09-27T17:59:50+00:00" and out["database_matches_archive"]
    stale_now = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)
    assert projections.collection(cfg, stale_now)["state"] == "stale"
    (state_dir / "history.json").write_text(
        json.dumps(
            {"version": 1, "database": "/elsewhere.sqlite3", "sequence": 1, "history": [entry]}
        )
    )
    out = projections.collection(cfg, NOW)
    assert out["state"] == "unavailable" and "different database" in out["reason"]


def test_overview_reports_unknown_cost_and_empty_farms(cfg):
    out = projections.overview(cfg, NOW)
    assert out["state"] == "ok"
    assert out["budgets"]["cost_usd"] is None and out["budgets"]["active_processes"] is None
    assert out["contracts"] == {"eligible": 2, "excluded": 0}
    assert out["coverage"] == {
        "markets": 2,
        "with_book": 2,
        "newest_book": "2026-09-27T15:00:08+00:00",
    }
    assert [g["game_id"] for g in out["games"]] == [824948, 824949]
    assert out["sources"] == {
        "archive": "ok",
        "games": "ok",
        "collection": "not_configured",
        "farms": "empty",
    }
    assert out["failures"] == []


def test_runs_for_a_game_come_only_from_its_own_pins(cfg):
    with CenterStore(cfg.root) as center:
        center.create_run(
            "11111111-1111-4111-8111-111111111111",
            "game_research",
            {"game_id": 824948, "event_ticker": EV1},
            "evidence_review_v1",
            {"max_tasks": 3},
            "farm-a",
        )
        center.create_run(
            "22222222-2222-4222-8222-222222222222",
            "game_research",
            {"game_id": 824949, "event_ticker": EV2},
            "evidence_review_v1",
            {"max_tasks": 3},
            "farm-b",
        )
    a = projections.game(cfg, 824948, NOW)
    b = projections.game(cfg, 824949, NOW)
    assert [r["run_id"][:1] for r in a["runs"]] == ["1"]
    assert [r["run_id"][:1] for r in b["runs"]] == ["2"]
    farms = projections.farms(cfg, NOW)
    assert farms["state"] == "ok" and len(farms["runs"]) == 2 and farms["active_processes"] is None
    assert projections.farm(cfg, "33333333-3333-4333-8333-333333333333", NOW)["state"] == "empty"
    act = projections.activity(cfg, "11111111-1111-4111-8111-111111111111", 1, NOW)
    assert act["state"] == "empty" and act["events"] == []


def test_forbidden_paths_refuse_configuration(tmp_path, dev_root):
    forbidden = tmp_path / "production"
    forbidden.mkdir()
    archive = forbidden / "archive.sqlite3"
    archive.touch()
    import pytest

    with pytest.raises(ValueError):
        CenterConfig(root=dev_root, archive=archive, forbidden_paths=(forbidden,))
    with pytest.raises(ValueError):
        CenterConfig(root=dev_root, host="0.0.0.0")
