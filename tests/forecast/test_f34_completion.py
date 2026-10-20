"""F3.4 completion: evidence-established predicates at C, renewal, vetoes, late
publication and the archive-only reconstruction (specification §2, §3, §4, §7).

All evidence is synthetic (injected transport and clock). The shipped fixture's game
starts 19:05Z, so C = 18:05Z; collections and ticks are placed around that cutoff.
"""

import copy
import json
from datetime import date, timedelta

import pytest
from _f34 import CUTOFF, GAME_ID, at, collect, fixture_data, protocol, tick_at, to_schema4
from conftest import Clock

from vader_intelligence.cli import execute, parser
from vader_intelligence.config import Config
from vader_intelligence.forecast import service
from vader_intelligence.forecast.reconstruct import reconstruct
from vader_intelligence.forecast.selection import select_inputs
from vader_intelligence.forecast.tick import inspect
from vader_intelligence.storage import Store

PROTO = protocol()
NS = PROTO.observer_namespace
GAME_DAY = date(2026, 9, 27)


def mutated(stage, transform):
    """Fixture data whose `stage` response body is transformed."""
    data = copy.deepcopy(fixture_data())
    for entry in data["responses"]:
        if entry["stage"] == stage:
            entry["body"] = json.dumps(transform(json.loads(entry["body"])))
    return data


def schedule_with(**game_fields):
    def transform(body):
        game = body["dates"][0]["games"][0]
        for key, value in game_fields.items():
            if isinstance(value, dict) and isinstance(game.get(key), dict):
                game[key].update(value)
            else:
                game[key] = value
        return body

    return mutated("schedule", transform)


def event_with(**market_fields):
    def transform(body):
        for market in body["markets"]:
            market.update(market_fields)
        return body

    return mutated("event", transform)


def handler_from(data):
    from test_collector import handler_for

    handler, _, _ = handler_for(data)
    return handler


def decision_rows(store):
    return [
        json.loads(r[0])
        for r in store.db.execute("SELECT payload_json FROM forecast_decisions ORDER BY id")
    ]


def early_binding(store, config):
    """A binding prepared an hour before C, from a collection at 17:00."""
    to_schema4(store)
    first = collect(store, config, origin=at(17, 0))
    warm = tick_at(store, PROTO, at(17, 0, 30))
    assert warm["bindings_prepared"] == 1
    return first


def test_renewal_uses_the_applicable_pass_at_cutoff_not_the_binding_evidence(store, config):
    early_binding(store, config)
    binding = json.loads(
        store.db.execute("SELECT payload_json FROM forecast_bindings").fetchone()[0]
    )
    second = collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_eligible"] == 1 and due["publications"]["allowed"] == 1
    (decision,) = decision_rows(store)
    renewal = decision["manifest"]["renewal"]
    assert decision["cutoff_eligible"] is True
    assert decision["manifest"]["discovery"]["id"] == second["discovery_pass_id"]
    assert renewal["schedule_fetch_id"] != binding["manifest"]["schedule_fetch_id"]
    assert renewal["market_fetch_id"] != binding["manifest"]["event_fetch_id"]
    assert renewal == renewal | {"identity": True, "pregame": True, "contract_valid": True}
    assert renewal["schedule_availability"] == "witnessed"
    assert decision["results"]["midpoint-v1"]["probability"] == "0.6450"
    assert store.verify_integrity() == []


def test_binding_not_renewed_by_cutoff_abstains_on_stale_evidence(store, config):
    early_binding(store, config)
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_abstained"] == 1 and due["decisions_eligible"] == 0
    (decision,) = decision_rows(store)
    assert decision["cutoff_eligible"] is False
    for reason in ("universe_incomplete", "discovery:input_stale", "schedule:input_stale"):
        assert reason in decision["reasons"]
    assert decision["results"]["constant-v1"]["probability"] is None
    assert decision["binding_id"] == 1  # the considered binding stays as provenance
    assert due["publications_pending_before"] == 0  # nothing to publish for an abstention


@pytest.mark.parametrize(
    "data,common,detail",
    [
        (
            schedule_with(gameDate="2026-09-27T19:35:00Z"),
            "not_pregame",
            "schedule_rule_time_mismatch",
        ),
        (
            schedule_with(status={"detailedState": "Postponed"}),
            "not_pregame",
            "not_scheduled_pregame",
        ),
        (schedule_with(rescheduledFrom="2026-09-26T19:05:00Z"), "not_pregame", "rescheduled_game"),
        (event_with(status="closed"), "contract_ineligible", "market_not_open"),
        (
            event_with(notional_value_dollars="0.5000"),
            "contract_ineligible",
            "unsupported_notional",
        ),
    ],
)
def test_latest_pre_cutoff_evidence_invalidates_eligibility_with_its_reason(
    store, config, data, common, detail
):
    """A start change, postponement, reschedule flag, closed market or changed notional
    observed before C abstains with the policy reason first and the evidence detail after
    it; the earlier favourable evidence does not repair it (specification §2 rule 4)."""
    early_binding(store, config)
    collect(store, config, origin=at(18, 3), handler=handler_from(data))
    tick_at(store, PROTO, at(18, 3, 30))
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_abstained"] == 1
    (decision,) = decision_rows(store)
    assert decision["cutoff_eligible"] is False
    assert common in decision["reasons"] and detail in decision["reasons"]
    assert decision["reasons"].index(common) < decision["reasons"].index(detail)
    assert decision["results"]["constant-v1"]["probability"] is None
    assert decision["manifest"]["renewal"]["reason"] == detail
    assert store.verify_integrity() == []


def test_game_missing_from_the_applicable_schedule_is_not_permission_to_reuse(store, config):
    early_binding(store, config)
    gone = mutated("schedule", lambda body: {"dates": []})
    collect(store, config, origin=at(18, 3), handler=handler_from(gone))
    tick_at(store, PROTO, at(18, 3, 30))
    tick_at(store, PROTO, at(18, 5, 30))
    (decision,) = decision_rows(store)
    assert decision["cutoff_eligible"] is False
    assert "schedule_missing_game" in decision["reasons"] and "not_pregame" in decision["reasons"]


def test_book_collected_under_another_schedule_version_is_rejected(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    ticker = "KXMLBGAME-26SEP271505HOUATH-HOU"
    ok = select_inputs(
        store.db,
        ticker=ticker,
        game_id=GAME_ID,
        cutoff=CUTOFF,
        namespace=NS,
        mode="synthetic",
        scheduled_start="2026-09-27T19:05:00+00:00",
    )
    assert ok.eligible
    revised = select_inputs(
        store.db,
        ticker=ticker,
        game_id=GAME_ID,
        cutoff=CUTOFF,
        namespace=NS,
        mode="synthetic",
        scheduled_start="2026-09-27T19:35:00+00:00",
    )
    assert revised.book is None and revised.reasons == ("book_schedule_mismatch",)


def test_post_cutoff_postponement_vetoes_publication_but_not_the_decision(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    # Post-cutoff evidence: the next collection, after C, sees the game postponed.
    postponed = schedule_with(status={"detailedState": "Postponed"})
    collect(store, config, origin=at(18, 5, 10), handler=handler_from(postponed))
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_eligible"] == 1
    assert due["publications"] == {"allowed": 0, "missed": 0, "vetoed": 1, "failed": 0}
    (decision,) = decision_rows(store)
    assert decision["cutoff_eligible"] is True  # frozen at C from pre-C evidence
    assert decision["results"]["midpoint-v1"]["probability"] == "0.6450"
    publication = json.loads(
        store.db.execute("SELECT payload_json FROM forecast_publications").fetchone()[0]
    )
    assert publication["verdict"] == "vetoed" and publication["reasons"] == [
        "not_scheduled_pregame"
    ]
    veto = publication["manifest"]["veto"]
    assert veto["reason"] == "not_scheduled_pregame"
    assert veto["observed_at"] > decision["cutoff"]
    assert {r["kind"] for r in publication["references"]} == {"schedule", "event"}
    # A retry returns the original verdict; no replacement publication.
    again = tick_at(store, PROTO, at(18, 6, 0))
    assert again["publications_pending_before"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM forecast_publications").fetchone()[0] == 1
    assert store.verify_integrity() == []


def test_veto_carries_the_witness_time_of_its_evidence(store, config):
    """Stack review F9: the veto's observation time is when the witness first observed
    the pass holding the vetoing eligibility decision, not the collector's own clock."""
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    postponed = schedule_with(status={"detailedState": "Postponed"})
    collect(store, config, origin=at(18, 5, 10), handler=handler_from(postponed))
    due = tick_at(store, PROTO, at(18, 5, 30))  # its first witness phase sees the pass
    assert due["publications"]["vetoed"] == 1
    veto, checked_at = store.db.execute(
        "SELECT json_extract(p.payload_json,'$.manifest.veto'), e.checked_at"
        " FROM forecast_publications p JOIN eligibility e"
        " ON e.id=json_extract(p.payload_json,'$.manifest.veto.eligibility_id')"
    ).fetchone()
    assert checked_at < at(18, 5, 30).isoformat()
    assert json.loads(veto)["observed_at"] == at(18, 5, 30).isoformat()


def test_unwitnessed_eligibility_row_never_vetoes_publication(store, config):
    """Stack review F9: only operational evidence of this tick's namespace and mode can
    veto. An eligibility row that no witness observed (here written by a run outside
    any witnessed pass) neither vetoes nor stamps its unwitnessed time into the record."""
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    (ticker,) = store.db.execute(
        "SELECT json_extract(payload_json,'$.ticker') FROM forecast_bindings"
    ).fetchone()
    from vader_intelligence.provenance import RunProvenanceV1

    stray = at(18, 5, 10).isoformat()
    store.db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "stray",
            "collect",
            stray,
            None,
            "interrupted",
            RunProvenanceV1(
                "stray", "collect", "a" * 64, {}, tool_configuration={"source_lineage": "synthetic"}
            ).to_json(),
            None,
        ),
    )
    store.db.execute(
        "INSERT INTO eligibility(run_id,ticker,checked_at,game_id,eligible,reason,data_json)"
        " VALUES ('stray',?,?,?,0,'not_scheduled_pregame','{}')",
        (ticker, stray, GAME_ID),
    )
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_eligible"] == 1
    assert due["publications"] == {"allowed": 1, "missed": 0, "vetoed": 0, "failed": 0}
    (manifest,) = store.db.execute(
        "SELECT json_extract(payload_json,'$.manifest') FROM forecast_publications"
    ).fetchone()
    assert "veto" not in json.loads(manifest)


def test_post_cutoff_evidence_that_keeps_the_game_pregame_does_not_veto(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    collect(store, config, origin=at(18, 5, 10))  # same evidence, still pregame
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["publications"]["allowed"] == 1 and due["publications"]["vetoed"] == 0


def test_inspect_explains_renewal_and_publication(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    tick_at(store, PROTO, at(18, 5, 30))
    tick_at(store, PROTO, at(18, 6, 0))  # witnesses the publication
    report = inspect(store, namespace=NS)
    (record,) = report["decisions"]
    considered = record["considered"]
    assert considered["binding_id"] == 1 and considered["renewal"]["identity"] is True
    assert considered["renewal"]["eligibility_id"] is not None
    assert record["publication"]["verdict"] == "allowed"
    assert record["publication"]["witnessed_at"] == at(18, 5, 30).isoformat()  # same tick
    assert record["publication"]["receipt_clock"] == "trusted"
    assert inspect(store, namespace=NS, dataset="other")["count"] == 0


# ------------------------------------------------------------------ reconstruct


def forward_slice(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, at(18, 3, 30))
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_eligible"] == 1


def reconstruct_at(store, moment, **kwargs):
    clock = Clock()
    clock.origin = moment
    return reconstruct(store, PROTO, clock=clock.now, monotonic=clock.mono, **kwargs)


def test_reconstruct_recomputes_the_cutoff_decision_without_publication(store, config):
    forward_slice(store, config)
    later = at(23, 0)
    result = reconstruct_at(
        store, later, dataset="study-1", start_date=GAME_DAY, through_date=GAME_DAY
    )
    assert result["status"] == "complete" and result["bindings_in_range"] == 1
    assert result["decisions_eligible"] == 1 and result["by_mode"] == {"synthetic": 1}
    assert result["publication"] == "not_applicable"
    forward, study = decision_rows(store)
    assert study["dataset"] == "study-1" and study["mode"] == "synthetic"
    assert study["created_at"] == later.isoformat()  # actual creation time, never C
    assert study["results"] == forward["results"]
    assert study["manifest"]["discovery"] == forward["manifest"]["discovery"]
    assert study["source_ceiling"] == result["source_ceiling"]
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM forecast_publications WHERE decision_id=2"
        ).fetchone()[0]
        == 0
    )
    # Idempotent: a repeat adds nothing; the run reports the existing decision.
    repeat = reconstruct_at(
        store, at(23, 30), dataset="study-1", start_date=GAME_DAY, through_date=GAME_DAY
    )
    assert repeat["decisions_existing"] == 1 and len(decision_rows(store)) == 2
    assert store.verify_integrity() == []
    assert inspect(store, namespace=NS, dataset="study-1")["count"] == 1


def test_reconstruct_refuses_the_forward_namespace_and_unbounded_ranges(store, config):
    forward_slice(store, config)
    with pytest.raises(ValueError, match="differ from the forward dataset"):
        reconstruct_at(
            store, at(23, 0), dataset=PROTO.dataset, start_date=GAME_DAY, through_date=GAME_DAY
        )
    with pytest.raises(ValueError, match="1..31 days"):
        reconstruct_at(
            store,
            at(23, 0),
            dataset="study-1",
            start_date=GAME_DAY,
            through_date=GAME_DAY + timedelta(days=31),
        )
    empty = reconstruct_at(
        store,
        at(23, 0),
        dataset="study-1",
        start_date=GAME_DAY + timedelta(days=1),
        through_date=GAME_DAY + timedelta(days=2),
    )
    assert empty["status"] == "inconclusive" and empty["bindings_in_range"] == 0
    assert len(decision_rows(store)) == 1


def test_reconstruct_source_ceiling_cannot_exclude_pre_cutoff_evidence(store, config):
    forward_slice(store, config)
    with pytest.raises(ValueError, match="excludes evidence retrieved before a cutoff"):
        reconstruct_at(
            store,
            at(23, 0),
            dataset="study-1",
            start_date=GAME_DAY,
            through_date=GAME_DAY,
            source_ceiling=1,
        )
    with pytest.raises(ValueError, match="within the archive"):
        reconstruct_at(
            store,
            at(23, 0),
            dataset="study-1",
            start_date=GAME_DAY,
            through_date=GAME_DAY,
            source_ceiling=10**9,
        )
    assert len(decision_rows(store)) == 1


def test_reconstruct_through_the_cli(tmp_path):
    path = tmp_path / "reconstruct.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)

    def cli(*args):
        return execute(parser().parse_args(["--db", str(path), *args]))

    assert cli("db-init")["schema_version"] == 1
    cli("settlement", "migrate")
    cli("forecast", "migrate")
    cli("forecast", "migrate", "--record-contract", "3")
    store = Store(path, min_free_bytes=1)
    try:
        collect(store, config, origin=at(18, 3))
        tick_at(store, PROTO, at(18, 3, 30))
        tick_at(store, PROTO, at(18, 5, 30))
    finally:
        store.close()
    result = cli(
        "forecast",
        "reconstruct",
        "--forward-dataset",
        PROTO.dataset,
        "--dataset",
        "study-cli",
        "--from",
        "2026-09-27",
        "--through",
        "2026-09-27",
    )
    assert result["status"] == "complete" and result["decisions_eligible"] == 1
    report = cli("forecast", "inspect", "--dataset", "study-cli")
    assert report["count"] == 1 and report["decisions"][0]["mode"] == "synthetic"
    replayed = cli("replay")
    assert replayed["forecast_storage"]["record_contracts"]["forecast_decisions"] == {
        "1": 0,
        "2": 0,
        "3": 2,
        "4": 0,
    }


def test_redecide_reports_renewal_and_reproduces(store, config):
    forward_slice(store, config)
    recomputed = service.redecide(store.db, 1, NS)
    assert recomputed["renewal"]["identity"] and recomputed["renewal"]["pregame"]
    assert recomputed["midpoint"] == recomputed["stored_midpoint"] == "0.6450"
