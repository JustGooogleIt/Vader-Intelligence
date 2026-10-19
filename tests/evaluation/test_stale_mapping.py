"""A postponed game must never be scored through a settlement mapping older than the
Kalshi result it is joined with (independent pre-review of #19). SYNTHETIC evidence."""

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


def test_a_failed_schedule_fetch_never_scores_a_postponed_game_with_an_old_mapping(
    store, config, monkeypatch
):
    import hashlib

    from conftest import Clock, response
    from test_materialization import EVENT, PDF, TICKER
    from test_transport import reader_for

    from vader_intelligence.settlement import journal as settlement_journal
    from vader_intelligence.settlement.service import SettlementRefresh

    forward_slice(store, config)
    pending = settlement_evidence(status="active")
    pending["schedule"]["dates"][0]["games"][0]["status"] = {
        "abstractGameState": "Preview",
        "detailedState": "Scheduled",
        "startTimeTBD": False,
    }
    settle(store, config, monkeypatch, pending, moment=at(19, 0))
    final = postponed(settlement_evidence())
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
            return response(data={"market": final["market"]})
        if path.endswith("/events/" + EVENT):
            return response(data={"event": final["event"]})
        if path.endswith("/schedule"):
            return response(status=503, body=b"unavailable")
        raise AssertionError(request.url)

    clock = Clock()
    clock.origin = at(23, 0)
    reader = reader_for(store, config, handler, clock)
    try:
        result = SettlementRefresh(store, reader, config).run(tickers=[TICKER])
    finally:
        reader.close()
    assert result["status"] == "partial"
    out = evaluate_at(store, at(23, 45), view="forward-shadow")
    assert out["report"]["scores"]["paired"]["n"] == 0
