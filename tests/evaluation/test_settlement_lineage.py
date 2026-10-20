"""Outcome labels follow the evaluation's source lineage (independent review F10, R9).

SYNTHETIC bytes throughout. A settlement run made with an injected transport is
synthetic; to model a live archive, one run here is recorded as live (`reader.live`).
A forward-shadow or reconstruction evaluation takes outcomes from live settlement runs
only, so synthetic bytes in the same archive can neither label nor shadow its outcome; a
synthetic evaluation may use either lineage.
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forecast"))
from _f34 import GAME_ID, at, to_schema4  # noqa: E402
from conftest import Clock, response  # noqa: E402
from test_materialization import (  # noqa: E402
    AS_OF,
    EVENT,
    PDF,
    PROTO,
    TICKER,
    settlement_evidence,
)
from test_transport import reader_for  # noqa: E402

from vader_intelligence.evaluation import outcomes  # noqa: E402
from vader_intelligence.evaluation.service import evaluate  # noqa: E402
from vader_intelligence.forecast import journal  # noqa: E402
from vader_intelligence.provenance import RunProvenanceV1  # noqa: E402
from vader_intelligence.settlement import journal as settlement_journal  # noqa: E402
from vader_intelligence.settlement.service import SettlementRefresh  # noqa: E402

STUDY = "study-live"
CODE = "a" * 64


def live_abstention(store):
    """A reconstruction-mode decision (abstaining, so no live evidence parents) for the
    fixture game, written by a run recorded as live."""
    store.db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "live-reconstruct",
            "forecast-reconstruct",
            at(23, 5).isoformat(),
            at(23, 6).isoformat(),
            "complete",
            RunProvenanceV1(
                "live-reconstruct",
                "forecast-reconstruct",
                CODE,
                {},
                tool_configuration={"source_lineage": "live"},
            ).to_json(),
            "{}",
        ),
    )
    journal.append(
        store.db,
        "forecast_decisions",
        {
            "schema_version": 1,
            "contract_version": 3,
            "run_id": "live-reconstruct",
            "created_at": at(23, 6).isoformat(),
            "code_hash": CODE,
            "config_hash": PROTO.config_hash,
            "mode": "historical_reconstruction",
            "references": [],
            "dataset": STUDY,
            "protocol": PROTO.protocol_id,
            "game_id": GAME_ID,
            "unresolved_key": None,
            "ticker": TICKER,
            "yes_team_id": 117,
            "horizon": 3600,
            "scheduled_start": "2026-09-27T19:05:00+00:00",
            "cutoff": "2026-09-27T18:05:00+00:00",
            "binding_id": None,
            "discovery_id": None,
            "selection_version": PROTO.selection_version,
            "baseline_versions": ["constant-v1", "midpoint-v1"],
            "cutoff_eligible": False,
            "reasons": ["synthetic_test_missing_inputs"],
            "results": {
                name: {"probability": None, "reasons": ["missing"], "inputs": {}}
                for name in ("constant-v1", "midpoint-v1")
            },
            "manifest": {},
            "source_ceiling": 0,
            "supersedes_id": None,
        },
    )


def refresh(store, config, monkeypatch, evidence, *, moment, live, market_status=200):
    """One settlement refresh of the fixture ticker; `live` records the run's lineage."""
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
            return response(market_status, data={"market": evidence["market"]})
        if path.endswith("/events/" + EVENT):
            return response(data={"event": evidence["event"]})
        if path.endswith("/schedule"):
            return response(data=evidence["schedule"])
        raise AssertionError(request.url)

    clock = Clock()
    clock.origin = moment
    reader = reader_for(store, config, handler, clock)
    reader.live = live
    try:
        return SettlementRefresh(store, reader, config).run(tickers=[TICKER])
    finally:
        reader.close()


def market_attempts(store):
    return store.db.execute(
        "SELECT f.id, f.state FROM fetches f JOIN requests r ON r.id=f.request_id"
        " WHERE r.stage='settlement-market' ORDER BY f.seq"
    ).fetchall()


def join(store, mode):
    return outcomes.join(
        store.db,
        ticker=TICKER,
        game_id=GAME_ID,
        yes_team_id=117,
        outcomes_as_of=AS_OF,
        source_ceiling=store.db.execute("SELECT MAX(seq) FROM fetches").fetchone()[0],
        scheduled_start=at(19, 5),
        mode=mode,
    )


def test_a_reconstruction_evaluation_takes_no_label_from_a_synthetic_settlement_run(
    store, config, monkeypatch
):
    to_schema4(store)
    live_abstention(store)
    synthetic = refresh(
        store, config, monkeypatch, settlement_evidence(), moment=at(23, 0), live=False
    )
    assert synthetic["status"] == "complete"
    # Not injected: a live evaluation run, as the CLI would write it.
    result = evaluate(
        store, PROTO, view="cutoff-reconstruction", dataset=STUDY, outcomes_as_of=AS_OF
    )
    report = result["report"]
    assert result["mode"] == "historical_reconstruction"
    assert report["outcome_status"] == {"outcome_not_observed": 1}
    assert report["outcome_reasons"] == {"no_settlement_attempt_in_view": 1}
    # The same bytes still label a synthetic join.
    assert join(store, "synthetic").status == "binary_scorable"
    assert join(store, "historical_reconstruction").status == "outcome_not_observed"
    assert join(store, "forward_shadow").status == "outcome_not_observed"


def test_a_later_synthetic_attempt_never_shadows_a_live_outcome(store, config, monkeypatch):
    to_schema4(store)
    live_abstention(store)
    live = refresh(store, config, monkeypatch, settlement_evidence(), moment=at(22, 0), live=True)
    assert live["status"] == "complete"
    # A later synthetic attempt fails (404): before the fix it was "the latest attempt"
    # and turned the live, finalized label into outcome_fetch_failed.
    failed = refresh(
        store,
        config,
        monkeypatch,
        settlement_evidence(),
        moment=at(23, 0),
        live=False,
        market_status=404,
    )
    assert failed["status"] == "partial"
    (live_fetch, _), (_, failed_state) = market_attempts(store)
    assert failed_state == "error"
    result = evaluate(
        store, PROTO, view="cutoff-reconstruction", dataset=STUDY, outcomes_as_of=AS_OF
    )
    assert result["report"]["outcome_status"] == {"binary_scorable": 1}
    joined = join(store, "historical_reconstruction")
    assert joined.scorable and joined.y == 1 and joined.fetch_id == live_fetch
    assert join(store, "synthetic").status == "outcome_fetch_failed"  # latest of any lineage


def test_a_later_synthetic_result_never_relabels_a_live_outcome(store, config, monkeypatch):
    to_schema4(store)
    live_abstention(store)
    refresh(store, config, monkeypatch, settlement_evidence(), moment=at(22, 0), live=True)
    refresh(
        store,
        config,
        monkeypatch,
        settlement_evidence(result="no", payout="0.0000"),
        moment=at(23, 0),
        live=False,
    )
    (live_fetch, _), (synthetic_fetch, _) = market_attempts(store)
    forward = join(store, "forward_shadow")
    assert forward.y == 1 and forward.fetch_id == live_fetch
    synthetic = join(store, "synthetic")
    assert synthetic.y == 0 and synthetic.fetch_id == synthetic_fetch
    result = evaluate(
        store, PROTO, view="cutoff-reconstruction", dataset=STUDY, outcomes_as_of=AS_OF
    )
    assert result["report"]["outcome_status"] == {"binary_scorable": 1}
    assert result["report"]["annotations"][f"game:{GAME_ID}"]["outcome_fetch_id"] == live_fetch
