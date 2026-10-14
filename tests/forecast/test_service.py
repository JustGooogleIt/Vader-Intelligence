"""Binding preparation, input selection and decision generation (F3.3b remainder).

All evidence is synthetic (injected transport and clock). Decisions are read through the
journal and the projection, not assembled directly.
"""

import json
from datetime import timedelta

import pytest
from _f34 import CUTOFF, GAME_ID, at, collect, protocol, to_schema4
from conftest import response
from test_collector import handler_for

from vader_intelligence.forecast import service
from vader_intelligence.forecast.selection import select_inputs
from vader_intelligence.forecast.witness import witness

PROTO = protocol()
NS = PROTO.observer_namespace


def witnessed_collection(store, config, *, origin=at(18, 3), handler=None, **kw):
    to_schema4(store)
    result = collect(store, config, origin=origin, handler=handler, **kw)
    # Witness the pass, fetches and any binding at a time after collection.
    from conftest import Clock

    clock = Clock()
    clock.origin = origin + timedelta(seconds=30)
    witness(store, clock=clock.now, monotonic=clock.mono, namespace=NS)
    return result


def prepare(store, result, *, created_at=None):
    created_at = created_at or (CUTOFF - timedelta(seconds=90)).isoformat()
    return service.prepare_binding(
        store,
        pass_id=result["discovery_pass_id"],
        game_id=GAME_ID,
        protocol=PROTO,
        run_id=result["run_id"],
        code_hash=store.db.execute(
            "SELECT json_extract(provenance_json,'$.code_hash') FROM runs WHERE id=?",
            (result["run_id"],),
        ).fetchone()[0],
        mode="synthetic",
        created_at=created_at,
    )


def test_prepare_binding_builds_full_identity(store, config):
    result = witnessed_collection(store, config)
    binding_id, digest, envelope = prepare(store, result)
    assert binding_id >= 1 and len(digest) == 64
    assert envelope["game_id"] == GAME_ID
    assert envelope["event_ticker"] == "KXMLBGAME-26SEP271505HOUATH"
    # Lower MLB team ID wins: Houston=117, Athletics=133 -> Houston (HOU).
    assert envelope["yes_team_id"] == 117
    assert envelope["ticker"].endswith("-HOU")
    assert envelope["team_ids"] == [117, 133]
    assert envelope["scheduled_start"] == "2026-09-27T19:05:00+00:00"
    # Binding cutoff is the forecast horizon C = start - 3600, NOT the collector T-120.
    assert envelope["cutoff"] == "2026-09-27T18:05:00+00:00"
    assert envelope["revision"] == 1 and envelope["previous_id"] is None
    assert len(envelope["terms_digest"]) == 64
    assert len(envelope["references"]) == 3  # event, schedule, terms fetches
    assert envelope["manifest"]["alternatives"] == [envelope["ticker"].replace("-HOU", "-ATH")]
    # The binding is a valid journal record and witnessable.
    assert store.verify_integrity() == []


def test_prepare_binding_is_idempotent_per_cohort(store, config):
    result = witnessed_collection(store, config)
    first, _, _ = prepare(store, result)
    # A second prepare of the same identity returns the existing binding (revision 1).
    second, _, _ = prepare(store, result, created_at=(CUTOFF - timedelta(seconds=80)).isoformat())
    assert first == second
    assert store.db.execute("SELECT COUNT(*) FROM forecast_bindings").fetchone()[0] == 1


def test_prepare_binding_refuses_ambiguous_identity(store, config):
    from _f34 import fixture_data

    data = fixture_data()
    event = json.loads(next(r["body"] for r in data["responses"] if r["stage"] == "event"))

    def one_bad(request):
        base, _, _ = handler_for(data)
        if "/events/" in request.url.path:
            broken = json.loads(json.dumps(event))
            broken["markets"][1]["rules_primary"] = "garbage rule that will not parse"
            return response(data=broken)
        return base(request)

    result = witnessed_collection(store, config, handler=one_bad)
    # Only one contract decided eligible, so the identity is not a provable two-contract pair.
    with pytest.raises(service.BindingUnavailable):
        prepare(store, result)


def test_select_inputs_picks_the_latest_witnessed_pre_cutoff_book(store, config):
    result = witnessed_collection(store, config)
    binding_id, _, envelope = prepare(store, result)
    ticker = envelope["ticker"]
    inputs = select_inputs(
        store.db, ticker=ticker, game_id=GAME_ID, cutoff=CUTOFF, namespace=NS, mode="synthetic"
    )
    assert inputs.eligible and inputs.book is not None
    assert inputs.book.observed_at <= CUTOFF
    assert inputs.book.retrieved_at <= CUTOFF
    assert "orderbook_fp" in inputs.book.raw


def test_select_inputs_missing_when_no_book(store, config):
    to_schema4(store)
    inputs = select_inputs(
        store.db, ticker="KXMLBGAME-NONE", game_id=1, cutoff=CUTOFF, namespace=NS, mode="synthetic"
    )
    assert inputs.book is None and inputs.reasons == ("book_missing",)


def test_select_inputs_book_after_cutoff_is_not_selected(store, config):
    result = witnessed_collection(store, config)
    _, _, envelope = prepare(store, result)
    ticker = envelope["ticker"]
    # A cutoff earlier than the book's retrieval: the pre-C set is empty.
    early = CUTOFF - timedelta(hours=1)
    inputs = select_inputs(
        store.db, ticker=ticker, game_id=GAME_ID, cutoff=early, namespace=NS, mode="synthetic"
    )
    assert inputs.book is None and inputs.reasons == ("book_missing",)


def test_select_inputs_unproven_when_book_not_witnessed(store, config):
    to_schema4(store)
    result = collect(store, config, origin=at(18, 3))  # collected, NOT witnessed
    _, _, envelope = prepare_unwitnessed(store, result)
    ticker = envelope["ticker"]
    inputs = select_inputs(
        store.db, ticker=ticker, game_id=GAME_ID, cutoff=CUTOFF, namespace=NS, mode="synthetic"
    )
    assert inputs.book is None and inputs.reasons == ("book_availability_unproven",)


def prepare_unwitnessed(store, result):
    code_hash = store.db.execute(
        "SELECT json_extract(provenance_json,'$.code_hash') FROM runs WHERE id=?",
        (result["run_id"],),
    ).fetchone()[0]
    return service.prepare_binding(
        store,
        pass_id=result["discovery_pass_id"],
        game_id=GAME_ID,
        protocol=PROTO,
        run_id=result["run_id"],
        code_hash=code_hash,
        mode="synthetic",
        created_at=(CUTOFF - timedelta(seconds=90)).isoformat(),
    )


def test_select_inputs_stale_book(store, config):
    result = witnessed_collection(store, config, origin=at(17, 57))  # book retrieved ~17:57
    _, _, envelope = prepare(store, result, created_at=(CUTOFF - timedelta(seconds=90)).isoformat())
    inputs = select_inputs(
        store.db,
        ticker=envelope["ticker"],
        game_id=GAME_ID,
        cutoff=CUTOFF,
        namespace=NS,
        mode="synthetic",
    )
    # ~480 s old at C.
    assert inputs.book is None and inputs.reasons == ("book_stale",)


def test_decide_eligible_produces_constant_and_midpoint(store, config):
    result = witnessed_collection(store, config)
    binding_id, _, _ = prepare(store, result)
    # Witness the freshly-prepared binding before C so it is available at the cutoff.
    from conftest import Clock

    clock = Clock()
    clock.origin = CUTOFF - timedelta(seconds=60)
    witness(store, clock=clock.now, monotonic=clock.mono, namespace=NS)
    decision = service.decide_opportunity(
        store,
        binding_id=binding_id,
        protocol=PROTO,
        run_id=result["run_id"],
        code_hash=store.db.execute(
            "SELECT json_extract(provenance_json,'$.code_hash') FROM runs WHERE id=?",
            (result["run_id"],),
        ).fetchone()[0],
        mode="synthetic",
        created_at=(CUTOFF + timedelta(seconds=30)).isoformat(),
        namespace=NS,
    )
    assert decision.cutoff_eligible is True and decision.reasons == ()
    assert decision.constant_probability == "0.5"
    assert decision.midpoint_probability == "0.6450"
    stored = json.loads(
        store.db.execute(
            "SELECT payload_json FROM forecast_decisions WHERE id=?", (decision.decision_id,)
        ).fetchone()[0]
    )
    assert stored["cutoff_eligible"] and stored["binding_id"] == binding_id
    assert stored["manifest"]["discovery"]["collector_config_hash"] != stored["config_hash"]
    assert store.verify_integrity() == []
    # Redecide reproduces from the same persisted inputs.
    recomputed = service.redecide(store.db, decision.decision_id, NS)
    assert recomputed["midpoint"] == "0.6450" and recomputed["stored_midpoint"] == "0.6450"


def test_decide_abstains_when_binding_not_witnessed_by_cutoff(store, config):
    result = witnessed_collection(store, config)
    binding_id, _, _ = prepare(store, result)
    # Witness the binding AFTER C: its observed_at exceeds the cutoff.
    from conftest import Clock

    clock = Clock()
    clock.origin = CUTOFF + timedelta(seconds=120)
    witness(store, clock=clock.now, monotonic=clock.mono, namespace=NS)
    decision = service.decide_opportunity(
        store,
        binding_id=binding_id,
        protocol=PROTO,
        run_id=result["run_id"],
        code_hash=store.db.execute(
            "SELECT json_extract(provenance_json,'$.code_hash') FROM runs WHERE id=?",
            (result["run_id"],),
        ).fetchone()[0],
        mode="synthetic",
        created_at=(CUTOFF + timedelta(seconds=150)).isoformat(),
        namespace=NS,
    )
    assert decision.cutoff_eligible is False
    assert (
        "binding_after_cutoff" in decision.reasons
        or "binding_availability_unproven" in decision.reasons
    )
    assert decision.constant_probability is None
    stored = json.loads(
        store.db.execute(
            "SELECT payload_json FROM forecast_decisions WHERE id=?", (decision.decision_id,)
        ).fetchone()[0]
    )
    # An abstention keeps the binding it considered as provenance (input IDs are
    # preserved for rejected inputs); only the eligibility and probabilities are absent.
    assert stored["binding_id"] == binding_id and stored["reasons"]
