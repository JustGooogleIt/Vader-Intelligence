"""A postponed game must never be scored through a settlement mapping older than the
Kalshi result it is joined with (independent pre-review of #19); a game proven final by
complete earlier evidence is not dropped when a later refresh's schedule fetch fails
(review F50). SYNTHETIC evidence."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forecast"))
from _f34 import at  # noqa: E402
from test_materialization import (  # noqa: E402
    evaluate_at,
    forward_slice,
    settle,
    settlement_evidence,
)
from test_schedule_revisions import postponed  # noqa: E402


def test_a_ceiling_between_the_market_and_schedule_fetches_never_scores_a_moved_game(
    store, config, monkeypatch
):
    forward_slice(store, config)  # eligible decision for start 19:05Z, C = 18:05Z, published
    pending = settlement_evidence(status="active")
    pending["schedule"]["dates"][0]["games"][0]["status"] = {
        "abstractGameState": "Preview",
        "detailedState": "Scheduled",
        "startTimeTBD": False,
    }
    settle(store, config, monkeypatch, pending, moment=at(19, 0))  # mapping rev 1: start 19:05Z
    settle(store, config, monkeypatch, postponed(settlement_evidence()))  # final, played next day
    rows = store.db.execute(
        "SELECT f.seq, r.stage FROM fetches f JOIN requests r ON r.id=f.request_id"
        " WHERE r.stage LIKE 'settlement-%' ORDER BY f.seq"
    ).fetchall()
    market_seq = max(s for s, st in rows if st == "settlement-market")
    full = evaluate_at(store, at(23, 45), view="forward-shadow")
    assert full["report"]["scores"]["paired"]["n"] == 0  # complete evidence: start moved
    cut = evaluate_at(store, at(23, 50), view="forward-shadow", source_ceiling=market_seq)
    assert cut["report"]["scores"]["paired"]["n"] == 0, "postponed game scored via stale mapping"


def refresh_without_schedule(store, config, monkeypatch, evidence, moment=at(23, 0), live=False):
    """One settlement refresh of the fixture ticker whose MLB schedule fetch gets a 503:
    the run records the Kalshi result but no mapping (a `partial` run). `live` records
    the run's lineage."""
    import hashlib

    from conftest import Clock, response
    from test_materialization import EVENT, PDF, TICKER
    from test_transport import reader_for

    from vader_intelligence.settlement import journal as settlement_journal
    from vader_intelligence.settlement.service import SettlementRefresh

    monkeypatch.setattr(settlement_journal, "REVIEWED_TERMS", {hashlib.sha256(PDF).hexdigest()})

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
            return response(data={"market": evidence["market"]})
        if path.endswith("/events/" + EVENT):
            return response(data={"event": evidence["event"]})
        if path.endswith("/schedule"):
            return response(status=503, body=b"unavailable")
        raise AssertionError(request.url)

    clock = Clock()
    clock.origin = moment
    reader = reader_for(store, config, handler, clock)
    reader.live = live
    try:
        result = SettlementRefresh(store, reader, config).run(tickers=[TICKER])
    finally:
        reader.close()
    assert result["status"] == "partial"
    return result


def preview(evidence):
    """The schedule still shows the game before first pitch (start 19:05Z)."""
    evidence["schedule"]["dates"][0]["games"][0]["status"] = {
        "abstractGameState": "Preview",
        "detailedState": "Scheduled",
        "startTimeTBD": False,
    }
    return evidence


def test_a_failed_schedule_fetch_never_scores_a_postponed_game_with_an_old_mapping(
    store, config, monkeypatch
):
    forward_slice(store, config)
    settle(store, config, monkeypatch, preview(settlement_evidence(status="active")), at(19, 0))
    refresh_without_schedule(store, config, monkeypatch, postponed(settlement_evidence()))
    out = evaluate_at(store, at(23, 45), view="forward-shadow")
    assert out["report"]["scores"]["paired"]["n"] == 0


def join_fixture(store, **kwargs):
    from _f34 import GAME_ID
    from test_materialization import AS_OF, TICKER

    from vader_intelligence.evaluation import outcomes

    arguments = {
        "ticker": TICKER,
        "game_id": GAME_ID,
        "yes_team_id": 117,
        "outcomes_as_of": AS_OF,
        "source_ceiling": store.db.execute("SELECT MAX(seq) FROM fetches").fetchone()[0],
        "scheduled_start": at(19, 5),
        "mode": "synthetic",
    }
    return outcomes.join(store.db, **(arguments | kwargs))


def mapping_ids(store):
    return [
        r[0] for r in store.db.execute("SELECT id FROM settlement_versions WHERE kind='mapping'")
    ]


def test_a_scored_game_stays_scored_after_a_refresh_whose_schedule_fetch_failed(
    store, config, monkeypatch
):
    """Review F50: a game proven by complete earlier evidence (a mapping derived from a
    schedule showing it Final, the same finalized Kalshi result) is not dropped from the
    paired population when a later routine refresh gets a 503 from the MLB schedule."""
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence(), at(22, 0))
    before = evaluate_at(store, at(22, 30), outcomes_as_of=at(22, 15), view="forward-shadow")
    assert before["report"]["scores"]["paired"]["n"] == 1
    refresh_without_schedule(store, config, monkeypatch, settlement_evidence(), at(23, 0))
    latest_market = store.db.execute(
        "SELECT f.id FROM fetches f JOIN requests r ON r.id=f.request_id"
        " WHERE r.stage='settlement-market' ORDER BY f.seq DESC LIMIT 1"
    ).fetchone()[0]
    joined = join_fixture(store)
    assert joined.status == "binary_scorable" and joined.y == 1
    assert joined.fetch_id == latest_market  # the label is still the latest attempt's
    assert joined.mapping_id == mapping_ids(store)[0]  # the complete earlier mapping
    after = evaluate_at(store, at(23, 45), view="forward-shadow")
    assert after["report"]["scores"]["paired"]["n"] == 1, "score vanished after a 503"


def test_the_fallback_mapping_must_come_from_a_schedule_showing_the_game_final(
    store, config, monkeypatch
):
    """The earlier mapping is used only when it cannot carry a stale start: one derived
    from a schedule still showing the game before first pitch leaves the identity
    unresolved, even with the same finalized Kalshi result."""
    forward_slice(store, config)
    settle(store, config, monkeypatch, preview(settlement_evidence()), at(21, 0))
    refresh_without_schedule(store, config, monkeypatch, settlement_evidence(), at(23, 0))
    joined = join_fixture(store)
    assert (joined.status, joined.reasons) == ("identity_unresolved", ("mapping_incomplete",))


def test_the_fallback_mapping_needs_the_same_finalized_kalshi_result(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence(status="active"), at(21, 0))
    refresh_without_schedule(store, config, monkeypatch, settlement_evidence(), at(23, 0))
    joined = join_fixture(store)
    assert (joined.status, joined.reasons) == ("identity_unresolved", ("mapping_incomplete",))
    # A different finalized result in the earlier run is no proof either.
    other = settlement_evidence(result="no", payout="0.0000", winner=133)
    settle(store, config, monkeypatch, other, at(23, 5))
    refresh_without_schedule(store, config, monkeypatch, settlement_evidence(), at(23, 10))
    joined = join_fixture(store)
    assert (joined.status, joined.reasons) == ("identity_unresolved", ("mapping_incomplete",))


def test_newer_contrary_complete_evidence_is_never_skipped(store, config, monkeypatch):
    """Only the most recent earlier run with complete mapping evidence is considered: a
    quarantining mapping between a good one and the failed refresh ends the search."""
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence(), at(21, 0))
    reversed_teams = settlement_evidence()
    game = reversed_teams["schedule"]["dates"][0]["games"][0]
    game["teams"]["away"], game["teams"]["home"] = game["teams"]["home"], game["teams"]["away"]
    settle(store, config, monkeypatch, reversed_teams, at(22, 0))
    refresh_without_schedule(store, config, monkeypatch, settlement_evidence(), at(23, 0))
    joined = join_fixture(store)
    assert (joined.status, joined.reasons) == ("identity_unresolved", ("mapping_incomplete",))


def test_the_fallback_follows_the_evaluation_lineage(store, config, monkeypatch):
    """A live evaluation never takes the fallback mapping from a synthetic run (F10)."""
    from test_settlement_lineage import refresh

    forward_slice(store, config)
    refresh(store, config, monkeypatch, settlement_evidence(), moment=at(22, 0), live=False)
    refresh_without_schedule(
        store, config, monkeypatch, settlement_evidence(), at(23, 0), live=True
    )
    live = join_fixture(store, mode="historical_reconstruction")
    assert (live.status, live.reasons) == ("identity_unresolved", ("mapping_incomplete",))
    assert join_fixture(store, mode="synthetic").status == "binary_scorable"
