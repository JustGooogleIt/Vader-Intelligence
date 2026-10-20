"""F3.5 evaluation materialization lifecycle (specification §5, §6, §7).

Synthetic throughout: collection, ticks, settlement refresh and evaluation all run with
an injected transport and clock on disposable archives. The shipped fixture's game
(824948, HOU @ ATH, 19:05Z) is forecast at C = 18:05Z and settled later the same day.
"""

import copy
import hashlib
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# The shared F3.4 builders live beside the forecast tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forecast"))
from _f34 import GAME_ID, at, collect, fixture_data, protocol, tick_at, to_schema4
from conftest import Clock, response
from test_transport import reader_for

from vader_intelligence.cli import execute, parser
from vader_intelligence.config import Config
from vader_intelligence.evaluation import outcomes
from vader_intelligence.evaluation.service import evaluate, inspect_evaluation, list_evaluations
from vader_intelligence.forecast import journal, service
from vader_intelligence.forecast.reconstruct import reconstruct
from vader_intelligence.forecast.references import digest
from vader_intelligence.provenance import RunProvenanceV1
from vader_intelligence.settlement import journal as settlement_journal
from vader_intelligence.settlement.service import SettlementRefresh
from vader_intelligence.storage import Store

PROTO = protocol()
NS = PROTO.observer_namespace
TICKER = "KXMLBGAME-26SEP271505HOUATH-HOU"
EVENT = "KXMLBGAME-26SEP271505HOUATH"
PDF = b"%PDF-1.4\nsynthetic fixture\n%%EOF"
GAME_DAY = date(2026, 9, 27)
SETTLED_AT = at(23, 0)
AS_OF = at(23, 30)


def settlement_evidence(*, result="yes", payout="1.0000", status="finalized", winner=117):
    data = fixture_data()
    event_body = json.loads(next(r["body"] for r in data["responses"] if r["stage"] == "event"))
    schedule = json.loads(next(r["body"] for r in data["responses"] if r["stage"] == "schedule"))
    market = copy.deepcopy(next(m for m in event_body["markets"] if m["ticker"] == TICKER))
    market.update(
        status=status,
        result=result,
        settlement_value_dollars=payout,
        settlement_ts="2026-09-27T22:30:00Z",
        updated_time="2026-09-27T22:30:01Z",
    )
    if status != "finalized":
        # A not-yet-settled market reports an empty result and no settlement fields.
        market.pop("settlement_ts")
        market.pop("settlement_value_dollars")
        market["result"] = ""
    game = schedule["dates"][0]["games"][0]
    game["status"] = {"abstractGameState": "Final", "detailedState": "Final", "startTimeTBD": False}
    game["isTie"] = False
    for side in ("away", "home"):
        team = game["teams"][side]
        team["isWinner"] = team["team"]["id"] == winner
        team["score"] = 5 if team["isWinner"] else 2
    return {"market": market, "event": event_body["event"], "schedule": schedule}


def settle(store, config, monkeypatch, evidence, moment=SETTLED_AT):
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
            return response(data=evidence["schedule"])
        raise AssertionError(request.url)

    clock = Clock()
    clock.origin = moment
    reader = reader_for(store, config, handler, clock)
    try:
        result = SettlementRefresh(store, reader, config).run(tickers=[TICKER])
    finally:
        reader.close()
    assert result["status"] == "complete", result
    return result


def forward_slice(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_eligible"] == 1 and due["publications"]["allowed"] == 1
    return due


def evaluate_at(store, moment, **kwargs):
    clock = Clock()
    clock.origin = moment
    kwargs.setdefault("outcomes_as_of", AS_OF)
    return evaluate(store, PROTO, clock=clock.now, monotonic=clock.mono, **kwargs)


def evaluation_rows(store):
    return [
        json.loads(r[0])
        for r in store.db.execute("SELECT payload_json FROM evaluation_runs ORDER BY id")
    ]


def items_of(store, evaluation_id):
    return {
        row["opportunity_key"]: row
        for row in (
            json.loads(r[0])
            for r in store.db.execute(
                "SELECT payload_json FROM evaluation_items WHERE evaluation_id=?", (evaluation_id,)
            )
        )
    }


# ------------------------------------------------------------------ scoring views


def test_forward_view_scores_only_timely_published_decisions(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    result = evaluate_at(store, at(23, 40), view="forward-shadow")
    assert result["status"] == "complete" and result["existing"] is False
    report = result["report"]
    assert report["population"] == report["population"] | {
        "U": 1,
        "E": 1,
        "K": 1,
        "M": 1,
        "P": 1,
        "FK": 1,
        "FM": 1,
        "FP": 1,
        "scored_paired": 1,
    }
    assert report["coverage"]["publication"]["FP/P"]["value"] == "1"
    paired = report["scores"]["paired"]
    assert paired["n"] == 1
    assert Decimal(paired["mean_brier_constant"]) == Decimal("0.25")
    assert Decimal(paired["mean_brier_midpoint"]) == Decimal("0.126025")  # (0.6450 - 1)^2
    assert Decimal(paired["mean_brier_delta"]) == Decimal("-0.123975")  # negative favours midpoint
    assert paired["mean_log_loss_constant"].startswith("0.69314718055994530941")
    assert paired["mean_log_loss_midpoint"].startswith("0.43850496")  # -ln(0.6450)
    assert report["collector_config_hash"] != report["research_config_hash"]
    assert report["evidence_mode"] == "synthetic" and report["simulated_view"] is True
    assert "synthetic_evidence_is_not_trading_edge" in report["provenance_limitations"]
    item = items_of(store, result["evaluation_id"])[f"game:{GAME_ID}"]
    assert item["y"] == 1 and item["payout"] == "1.0000"
    assert item["publication_status"] == "published_timely" and item["receipt_id"]
    assert item["outcome_status"] == "binary_scorable"
    assert item["mapping_id"] and item["mlb_id"] and item["kalshi_id"]
    assert report["annotations"][f"game:{GAME_ID}"]["mlb_agrees"] is True
    assert Decimal(item["scores"]["midpoint-v1"]["brier"]) == Decimal("0.126025")
    assert Decimal(item["paired_delta"]["brier"]) == Decimal("-0.123975")
    assert item["clipped"] == {"constant-v1": False, "midpoint-v1": False}
    assert store.verify_integrity() == []


def test_reconstruction_view_scores_cutoff_population_without_publication(
    store, config, monkeypatch
):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    study = reconstruct(
        store,
        PROTO,
        dataset="study-1",
        start_date=GAME_DAY,
        through_date=GAME_DAY,
        clock=lambda: at(23, 10),
        monotonic=lambda: 0.0,
    )
    assert study["decisions_eligible"] == 1
    result = evaluate_at(store, at(23, 45), view="cutoff-reconstruction", dataset="study-1")
    report = result["report"]
    assert result["status"] == "complete"
    assert report["population"]["P"] == 1 and report["population"]["scored_paired"] == 1
    assert report["coverage"]["publication"] is None
    assert report["publication_status"] == {"not_applicable": 1}
    assert Decimal(report["scores"]["paired"]["mean_brier_midpoint"]) == Decimal("0.126025")
    # The forward dataset cannot be evaluated in the reconstruction view with its own
    # synthetic decisions unless simulated; the view is labelled in the report.
    assert report["view"] == "cutoff-reconstruction"


def test_vetoed_publication_is_coverage_not_score_in_forward_view(store, config, monkeypatch):
    from test_f34_completion import handler_from, schedule_with

    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    collect(
        store,
        config,
        origin=at(18, 5, 10),
        handler=handler_from(schedule_with(status={"detailedState": "Postponed"})),
    )
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["publications"]["vetoed"] == 1
    settle(store, config, monkeypatch, settlement_evidence())
    forward = evaluate_at(store, at(23, 40), view="forward-shadow")
    population = forward["report"]["population"]
    assert population["K"] == 1 and population["FK"] == 0 and population["scored_paired"] == 0
    assert forward["report"]["publication_status"] == {"vetoed": 1}
    assert forward["report"]["publication_reasons"] == {"not_scheduled_pregame": 1}
    assert forward["status"] == "inconclusive"
    # The cutoff population is unchanged in the reconstruction view and can be scored.
    reconstruct(
        store,
        PROTO,
        dataset="study-1",
        start_date=GAME_DAY,
        through_date=GAME_DAY,
        clock=lambda: at(23, 10),
        monotonic=lambda: 0.0,
    )
    study = evaluate_at(store, at(23, 45), view="cutoff-reconstruction", dataset="study-1")
    assert study["report"]["population"]["scored_paired"] == 1


# ------------------------------------------------------------------ outcome lifecycle


def test_pending_then_settled_outcome_supersedes_without_rewriting(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence(status="active"), moment=at(22, 0))
    first = evaluate_at(store, at(22, 30), view="forward-shadow", outcomes_as_of=at(22, 20))
    assert first["status"] == "inconclusive"
    assert first["report"]["outcome_status"] == {"pending": 1}
    first_digest = store.db.execute(
        "SELECT digest FROM evaluation_runs WHERE id=?", (first["evaluation_id"],)
    ).fetchone()[0]
    settle(store, config, monkeypatch, settlement_evidence())
    second = evaluate_at(store, at(23, 40), view="forward-shadow")
    assert second["status"] == "complete" and second["supersedes_id"] == first["evaluation_id"]
    assert second["report"]["supersedes"]["reason"].startswith("outcome_evidence_changed")
    rows = evaluation_rows(store)
    assert len(rows) == 2 and rows[0]["status"] == "inconclusive"
    assert (
        store.db.execute(
            "SELECT digest FROM evaluation_runs WHERE id=?", (first["evaluation_id"],)
        ).fetchone()[0]
        == first_digest
    )
    inspected = inspect_evaluation(store.db, first["evaluation_id"])
    assert inspected["superseded_by"] == [second["evaluation_id"]]
    assert store.verify_integrity() == []


def test_a_b_a_corrections_retain_three_evaluations(store, config, monkeypatch):
    forward_slice(store, config)
    ids = []
    for n, (result, payout) in enumerate([("yes", "1.0000"), ("no", "0.0000"), ("yes", "1.0000")]):
        settle(
            store,
            config,
            monkeypatch,
            settlement_evidence(result=result, payout=payout),
            moment=at(22, 0) + timedelta(minutes=10 * n),
        )
        evaluation = evaluate_at(
            store,
            at(22, 1) + timedelta(minutes=10 * n),
            view="forward-shadow",
            outcomes_as_of=at(22, 1) + timedelta(minutes=10 * n),
        )
        ids.append(evaluation["evaluation_id"])
    rows = evaluation_rows(store)
    assert [r["supersedes_id"] for r in rows] == [None, ids[0], ids[1]]
    labels = [items_of(store, i)[f"game:{GAME_ID}"]["y"] for i in ids]
    assert labels == [1, 0, 1]
    briers = [
        Decimal(items_of(store, i)[f"game:{GAME_ID}"]["scores"]["midpoint-v1"]["brier"])
        for i in ids
    ]
    assert briers == [Decimal("0.126025"), Decimal("0.416025"), Decimal("0.126025")]
    # Three immutable versions although A's payout reappears: the evidence differs.
    assert len({r["outcome_digest"] for r in rows}) == 3
    assert store.verify_integrity() == []


@pytest.mark.parametrize(
    "changes,status,payout",
    [
        (dict(result="scalar", payout="0.5300"), "exceptional_payout", "0.5300"),
        (dict(status="disputed"), "disputed", None),
        (dict(result="yes", payout="0.0000"), "exceptional_payout", "0.0000"),
    ],
)
def test_ineligible_outcomes_are_visible_and_unscored(
    store, config, monkeypatch, changes, status, payout
):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence(**changes))
    result = evaluate_at(store, at(23, 40), view="forward-shadow")
    assert result["status"] == "inconclusive"
    assert result["report"]["outcome_status"] == {status: 1}
    item = items_of(store, result["evaluation_id"])[f"game:{GAME_ID}"]
    assert item["y"] is None and item["payout"] == payout
    assert item["scores"] == {"constant-v1": None, "midpoint-v1": None}
    assert item["publication_status"] == "published_timely"  # publication coverage intact


def test_later_evidence_after_the_boundary_is_invisible(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    early = evaluate_at(store, at(23, 40), view="forward-shadow", outcomes_as_of=at(22, 0))
    assert early["report"]["outcome_status"] == {"outcome_not_observed": 1}
    late = evaluate_at(store, at(23, 41), view="forward-shadow", outcomes_as_of=AS_OF)
    assert late["report"]["outcome_status"] == {"binary_scorable": 1}
    assert late["supersedes_id"] == early["evaluation_id"]


def test_mlb_disagreement_is_an_annotation_not_a_label(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence(result="yes", winner=133))
    result = evaluate_at(store, at(23, 40), view="forward-shadow")
    item = items_of(store, result["evaluation_id"])[f"game:{GAME_ID}"]
    assert item["y"] == 1  # Kalshi's finalized label stands
    assert result["report"]["annotations"][f"game:{GAME_ID}"]["mlb_agrees"] is False
    assert result["report"]["mlb_annotation"] == {"False": 1}


# ------------------------------------------------------------------ population


def test_unattempted_binding_is_counted_with_a_labelled_annotation(store, config, monkeypatch):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))  # binding prepared and witnessed; no due tick
    settle(store, config, monkeypatch, settlement_evidence())
    result = evaluate_at(store, at(23, 40), view="forward-shadow")
    report = result["report"]
    assert report["population"]["U"] == 1 and report["population"]["unattempted"] == 1
    assert report["population"]["E"] == 0 and report["population"]["FK"] == 0
    assert report["population"]["annotated_eligible_unattempted"] == 1
    annotation = report["annotations"][f"game:{GAME_ID}"]["cutoff_policy_annotation"]
    assert annotation["cutoff_eligible"] is True and annotation["midpoint_available"] is True
    item = items_of(store, result["evaluation_id"])[f"game:{GAME_ID}"]
    assert item["decision_id"] is None and item["cutoff_status"] == "unattempted"
    assert item["publication_status"] == "not_attempted"
    assert item["outcome_status"] == "binary_scorable" and item["scores"]["constant-v1"] is None
    assert result["status"] == "inconclusive"


def test_game_without_binding_is_counted_from_the_frozen_manifest(store, config):
    from test_collector import handler_for

    to_schema4(store)
    data = fixture_data()
    event = json.loads(next(r["body"] for r in data["responses"] if r["stage"] == "event"))

    def one_bad(request):
        base, _, _ = handler_for(data)
        if "/events/" in request.url.path:
            broken = json.loads(json.dumps(event))
            broken["markets"][1]["rules_primary"] = "garbage rule that will not parse"
            return response(data=broken)
        return base(request)

    collect(store, config, origin=at(18, 3), handler=one_bad)
    warm = tick_at(store, PROTO, at(18, 3, 30))
    assert warm["bindings_prepared"] == 0 and warm["bindings_skipped"]
    result = evaluate_at(store, at(23, 40), view="forward-shadow")
    report = result["report"]
    assert report["population"]["no_binding"] == 1 and report["population"]["U"] == 1
    annotation = report["annotations"][f"game:{GAME_ID}"]
    # The rejected contract resolved no game, so it is an unresolved candidate, not a
    # reason on this game; the eligible contract alone was not a provable pair.
    assert annotation["eligibility_reasons"] == {"eligible": 1}
    assert report["population"]["unresolved_candidates"] == 1
    item = items_of(store, result["evaluation_id"])[f"game:{GAME_ID}"]
    assert item["cutoff_status"] == "no_binding" and item["outcome_status"] == "identity_unresolved"


def other_cohort_abstention(store, game_id=900002, start=at(20, 0)):
    """A second collector configuration wrote a pass that pins another decision (an
    abstention for `game_id` at `start`). Returns that cohort's collector hash."""
    other_run, other_config = "other-collector", {"max_pages": 7}
    store.db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            other_run,
            "collect",
            at(19, 0).isoformat(),
            at(19, 1).isoformat(),
            "complete",
            RunProvenanceV1(
                other_run,
                "collect",
                service.source_hash() if hasattr(service, "source_hash") else "a" * 64,
                other_config,
                tool_configuration={
                    "collection_protocol": "two-phase-v1",
                    "source_lineage": "synthetic",
                },
            ).to_json(),
            "{}",
        ),
    )
    code_hash = json.loads(
        store.db.execute("SELECT provenance_json FROM runs WHERE id=?", (other_run,)).fetchone()[0]
    )["code_hash"]
    pass_payload = {
        "schema_version": 1,
        "run_id": other_run,
        "created_at": at(19, 1).isoformat(),
        "code_hash": code_hash,
        "config_hash": digest(other_config),
        "mode": "synthetic",
        "references": [],
        "session_id": "other-session",
        "phase_sequence": 1,
        "completed_at": at(19, 1).isoformat(),
        "complete": True,
        "reasons": [],
        "counts": {"games": 0, "contracts": 0},
        "eligibility_after_id": 0,
        "eligibility_through_id": 0,
        "manifest": {"manifest_version": 1},
    }
    pass_id = journal.append(store.db, "discovery_passes", pass_payload)
    pass_digest = store.db.execute(
        "SELECT digest FROM discovery_passes WHERE id=?", (pass_id,)
    ).fetchone()[0]
    abstention = {
        "schema_version": 1,
        "contract_version": 3,
        "run_id": other_run,
        "created_at": at(19, 2).isoformat(),
        "code_hash": code_hash,
        "config_hash": PROTO.config_hash,
        "mode": "synthetic",
        "references": [],
        "dataset": PROTO.dataset,
        "protocol": PROTO.protocol_id,
        "game_id": game_id,
        "unresolved_key": None,
        "ticker": None,
        "yes_team_id": None,
        "horizon": 3600,
        "scheduled_start": start.isoformat(),
        "cutoff": (start - timedelta(hours=1)).isoformat(),
        "binding_id": None,
        "discovery_id": pass_id,
        "selection_version": PROTO.selection_version,
        "baseline_versions": ["constant-v1", "midpoint-v1"],
        "cutoff_eligible": False,
        "reasons": ["identity_unproven"],
        "results": {
            name: {"probability": None, "reasons": ["identity_unproven"], "inputs": {}}
            for name in ("constant-v1", "midpoint-v1")
        },
        "manifest": {
            "discovery": {
                "id": pass_id,
                "digest": pass_digest,
                "collector_config_hash": digest(other_config),
            }
        },
        "source_ceiling": 0,
        "supersedes_id": None,
    }
    journal.append(store.db, "forecast_decisions", abstention)
    return digest(other_config)


def test_mixed_collector_cohorts_are_evaluated_separately(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    other_config = {"max_pages": 7}
    assert other_cohort_abstention(store) == digest(other_config)
    with pytest.raises(ValueError, match="separate cohort"):
        evaluate_at(store, at(23, 40), view="forward-shadow")
    real_hash = json.loads(
        store.db.execute("SELECT payload_json FROM forecast_decisions WHERE id=1").fetchone()[0]
    )["manifest"]["discovery"]["collector_config_hash"]
    first = evaluate_at(store, at(23, 40), view="forward-shadow", collector_config_hash=real_hash)
    second = evaluate_at(
        store, at(23, 41), view="forward-shadow", collector_config_hash=digest(other_config)
    )
    assert (
        first["report"]["population"]["U"] == 1
        and first["report"]["population"]["scored_paired"] == 1
    )
    assert (
        second["report"]["population"]["U"] == 1
        and second["report"]["population"]["scored_paired"] == 0
    )
    assert second["report"]["population"]["unattempted"] == 0  # the real game was decided elsewhere
    assert (
        first["report"]["collector_cohorts_in_dataset"]
        == second["report"]["collector_cohorts_in_dataset"]
    )
    assert first["evaluation_id"] != second["evaluation_id"] and second["supersedes_id"] is None
    assert store.verify_integrity() == []


# ------------------------------------------------------------------ repeat, interruption, leakage


def test_repeat_with_identical_manifests_returns_the_existing_evaluation(
    store, config, monkeypatch
):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    first = evaluate_at(store, at(23, 40), view="forward-shadow")
    again = evaluate_at(store, at(23, 50), view="forward-shadow")
    assert again["existing"] is True and again["evaluation_id"] == first["evaluation_id"]
    assert store.db.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 1
    assert list_evaluations(store.db)["count"] == 1


def test_budget_exhaustion_writes_nothing_and_retry_succeeds(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    ticks = iter([0.0, 100.0])  # session baseline, then the first per-item check
    result = evaluate(
        store,
        PROTO,
        view="forward-shadow",
        outcomes_as_of=AS_OF,
        clock=lambda: at(23, 40),
        monotonic=lambda: next(ticks, 100.0),
        budget=5.0,
    )
    assert result["status"] == "partial" and result["evaluation_id"] is None
    assert store.db.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM evaluation_items").fetchone()[0] == 0
    retry = evaluate_at(store, at(23, 41), view="forward-shadow")
    assert retry["status"] == "complete" and retry["existing"] is False


def test_interrupted_materialization_leaves_no_half_report(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    real = journal.append_evaluation

    def crash(db, payload, items):
        raise KeyboardInterrupt()

    monkeypatch.setattr(journal, "append_evaluation", crash)
    with pytest.raises(KeyboardInterrupt):
        evaluate_at(store, at(23, 40), view="forward-shadow")
    assert store.db.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 0
    assert (
        store.db.execute("SELECT status FROM runs WHERE kind='evaluation'").fetchone()[0]
        == "failed"
    )
    monkeypatch.setattr(journal, "append_evaluation", real)
    retry = evaluate_at(store, at(23, 41), view="forward-shadow")
    assert retry["status"] == "complete" and store.verify_integrity() == []


def test_outcome_evidence_never_enters_forecast_selection(store, config, monkeypatch):
    forward_slice(store, config)
    (before,) = [
        json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
    ]
    settle(store, config, monkeypatch, settlement_evidence(result="no", payout="0.0000"))
    evaluate_at(store, at(23, 40), view="forward-shadow")
    recomputed = service.redecide(store.db, 1, NS)
    assert recomputed["midpoint"] == before["results"]["midpoint-v1"]["probability"] == "0.6450"
    study = reconstruct(
        store,
        PROTO,
        dataset="after-outcome",
        start_date=GAME_DAY,
        through_date=GAME_DAY,
        clock=lambda: at(23, 50),
        monotonic=lambda: 0.0,
    )
    assert study["decisions_eligible"] == 1
    after = json.loads(
        store.db.execute(
            "SELECT payload_json FROM forecast_decisions WHERE json_extract(payload_json,'$.dataset')='after-outcome'"
        ).fetchone()[0]
    )
    assert after["results"] == before["results"] and after["reasons"] == before["reasons"]
    assert after["references"] == before["references"]  # the book only; no settlement reference
    assert all(ref["kind"] == "book" for ref in after["references"])


def test_outcome_join_dispositions_directly(store, config, monkeypatch):
    forward_slice(store, config)
    assert (
        outcomes.join(
            store.db,
            ticker=None,
            game_id=GAME_ID,
            yes_team_id=117,
            outcomes_as_of=AS_OF,
            source_ceiling=10**6,
            mode="synthetic",
        ).status
        == "identity_unresolved"
    )
    assert (
        outcomes.join(
            store.db,
            ticker=TICKER,
            game_id=GAME_ID,
            yes_team_id=117,
            outcomes_as_of=AS_OF,
            source_ceiling=10**6,
            mode="synthetic",
        ).status
        == "outcome_not_observed"
    )
    settle(store, config, monkeypatch, settlement_evidence())
    joined = outcomes.join(
        store.db,
        ticker=TICKER,
        game_id=GAME_ID,
        yes_team_id=117,
        outcomes_as_of=AS_OF,
        source_ceiling=10**6,
        mode="synthetic",
    )
    assert joined.scorable and joined.y == 1 and joined.mlb_agrees is True
    # A frozen game that the mapping contradicts cannot score.
    changed = outcomes.join(
        store.db,
        ticker=TICKER,
        game_id=999,
        yes_team_id=117,
        outcomes_as_of=AS_OF,
        source_ceiling=10**6,
        mode="synthetic",
    )
    assert changed.status == "identity_changed"
    # A ceiling below the settlement attempt hides it.
    assert (
        outcomes.join(
            store.db,
            ticker=TICKER,
            game_id=GAME_ID,
            yes_team_id=117,
            outcomes_as_of=AS_OF,
            source_ceiling=1,
            mode="synthetic",
        ).status
        == "outcome_not_observed"
    )


def test_evaluate_and_inspect_through_the_cli(tmp_path, monkeypatch):
    path = tmp_path / "evaluate.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)

    def cli(*args):
        return execute(parser().parse_args(["--db", str(path), *args]))

    cli("db-init")
    cli("settlement", "migrate")
    cli("forecast", "migrate")
    cli("forecast", "migrate", "--record-contract", "3")
    store = Store(path, min_free_bytes=1)
    try:
        forward_slice(store, config)
        settle(store, config, monkeypatch, settlement_evidence())
    finally:
        store.close()
    research = tmp_path / "research.toml"
    research.write_text(f'[research]\ndataset = "{PROTO.dataset}"\n')
    result = cli(
        "evaluate",
        "--research-config",
        str(research),
        "--view",
        "forward-shadow",
        "--outcomes-as-of",
        AS_OF.isoformat(),
    )
    assert result["status"] == "complete" and result["evaluation_id"] == 1
    listing = cli("evaluation", "inspect")
    assert listing["count"] == 1 and listing["evaluations"][0]["paired"]["n"] == 1
    detail = cli("evaluation", "inspect", "--id", "1", "--items")
    assert detail["evaluation"]["view"] == "forward-shadow" and len(detail["items"]) == 1
    assert cli("replay")["forecast_storage"]["record_contracts"]["evaluation_runs"] == {
        "1": 1,
        "2": 0,
        "3": 0,
        "4": 0,
    }


def test_evaluate_through_the_cli_defaults_to_the_archive_record_contract(tmp_path, monkeypatch):
    """On schema 6 new decisions are contract 4; the CLI must evaluate them by default and
    accept --record-contract 4 (review F25), as `evaluate()` itself does."""
    path = tmp_path / "evaluate6.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)

    def cli(*args):
        return execute(parser().parse_args(["--db", str(path), *args]))

    cli("db-init")
    cli("settlement", "migrate")
    cli("forecast", "migrate")
    cli("forecast", "migrate", "--record-contract", "4")
    store = Store(path, min_free_bytes=1)
    try:
        assert store.schema_version() == 6
        collect(store, config, origin=at(18, 3))
        tick_at(store, PROTO, at(18, 3, 30))
        assert tick_at(store, PROTO, at(18, 5, 30))["decisions_eligible"] == 1
        settle(store, config, monkeypatch, settlement_evidence())
    finally:
        store.close()
    research = tmp_path / "research.toml"
    research.write_text(f'[research]\ndataset = "{PROTO.dataset}"\n')
    base = (
        "evaluate",
        "--research-config",
        str(research),
        "--view",
        "forward-shadow",
        "--outcomes-as-of",
        AS_OF.isoformat(),
    )
    result = cli(*base)
    report = result["report"]
    assert result["status"] == "complete" and report["record_contract"] == 4
    assert report["population"]["scored_paired"] == 1 and report["excluded_decisions"] == []
    explicit = cli(*base, "--record-contract", "4")
    assert explicit["existing"] is True and explicit["evaluation_id"] == result["evaluation_id"]
    legacy = cli(*base, "--record-contract", "3")
    assert legacy["status"] == "inconclusive" and legacy["report"]["record_contract"] == 3
    assert legacy["report"]["excluded_decisions"] == [
        {"id": 1, "contract": 4, "reason": "other_contract"}
    ]
    with pytest.raises(SystemExit):
        parser().parse_args(["evaluate", *base[1:], "--record-contract", "5"])
