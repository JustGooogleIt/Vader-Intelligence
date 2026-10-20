"""Midpoint book selection versions (decision D-016). All evidence is synthetic.

Specification §4 selects the latest completed book retrieval received **and** witnessed
by C. `selection-v1` anchored on the latest retrieval received by C instead, so under
the specified cycle order (tick, then collect) the midpoint abstained at almost every
cutoff. `selection-v2` is §4 as written; `selection-v1` stays the default, unchanged.
"""

import json

import pytest
from _f34 import CUTOFF, GAME_ID, at, collect, fixture_data, protocol, tick_at, to_schema4
from conftest import Clock, response
from test_collector import handler_for

from vader_intelligence.forecast import service
from vader_intelligence.forecast.research import Protocol
from vader_intelligence.forecast.selection import SELECTION_V1, SELECTION_V2, select_inputs
from vader_intelligence.forecast.witness import witness

V1 = protocol()
V2 = Protocol(dataset=V1.dataset, selection_version=SELECTION_V2)
NS = V1.observer_namespace
TICKER = "KXMLBGAME-26SEP271505HOUATH-HOU"  # the fixture's booked, selected contract


def witness_at(store, moment):
    clock = Clock()
    clock.origin = moment
    return witness(store, clock=clock.now, monotonic=clock.mono, namespace=NS)


def select(store, version, cutoff=CUTOFF):
    return select_inputs(
        store.db,
        ticker=TICKER,
        game_id=GAME_ID,
        cutoff=cutoff,
        namespace=NS,
        mode="synthetic",
        version=version,
    )


def book_times(store):
    return [
        r[0]
        for r in store.db.execute(
            "SELECT f.retrieved_at FROM fetches f JOIN requests r ON r.id=f.request_id"
            " WHERE r.stage='book' AND r.request_key LIKE ? ORDER BY f.retrieved_at",
            ("%" + TICKER,),
        )
    ]


def midpoint(store):
    (payload,) = [
        json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
    ]
    return payload["selection_version"], payload["results"]["midpoint-v1"]


def cycles(store, config, proto, *, extra_tick=None):
    """The specified cycle: a tick, then a collection, every 120 s; C = 18:05:00 falls
    10 s before the cycle that decides it."""
    to_schema4(store)
    for minute in (1, 3):
        tick_at(store, proto, at(18, minute, 0))
        collect(store, config, origin=at(18, minute, 1))
    if extra_tick is not None:
        tick_at(store, proto, extra_tick)
    return tick_at(store, proto, at(18, 5, 10))


def test_the_fixture_ticker_is_the_selected_contract(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    assert book_times(store), "TICKER must name the fixture's booked contract"


def test_v1_abstains_under_the_specified_cycle_order(store, config):
    due = cycles(store, config, V1)
    assert due["decisions_eligible"] == 1
    version, result = midpoint(store)
    # The latest book retrieved by C (18:03:04.5) is first witnessed at 18:05:10.
    assert version == SELECTION_V1
    assert result["probability"] is None
    assert result["reasons"] == ["book_availability_after_cutoff"]


def test_v2_selects_the_latest_book_witnessed_by_cutoff(store, config):
    due = cycles(store, config, V2)
    assert due["decisions_eligible"] == 1
    version, result = midpoint(store)
    assert version == SELECTION_V2
    assert result["probability"] == "0.6450" and result["reasons"] == []
    # The previous cycle's book: retrieved 18:01:04.5, witnessed by the 18:03:00 tick.
    assert result["inputs"]["retrieved_at"] == at(18, 1, 4).isoformat().replace(
        "+00:00", ".500000+00:00"
    )
    assert result["inputs"]["observed_at"] == at(18, 3, 0).isoformat()
    recomputed = service.redecide(store.db, 1, NS)
    assert recomputed["midpoint"] == recomputed["stored_midpoint"] == "0.6450"


def test_redecide_keeps_the_v1_rule_for_v1_decisions(store, config):
    cycles(store, config, V1)
    recomputed = service.redecide(store.db, 1, NS)
    assert recomputed["stored_midpoint"] is None and recomputed["midpoint"] is None


def test_both_versions_agree_when_a_tick_witnesses_the_latest_book_before_cutoff(store, config):
    # One extra tick at C - 1 s witnesses the 18:03 book before C; both rules pick it.
    cycles(store, config, V1, extra_tick=at(18, 4, 59))
    assert select(store, SELECTION_V1) == select(store, SELECTION_V2)
    assert select(store, SELECTION_V2).eligible


def test_spec_worked_example_newest_qualifying_book_is_stale(store, config):
    # Specification §4 example: the newest book witnessed before C is 350 s old, and a
    # newer book witnessed after C cannot rescue it.
    to_schema4(store)
    collect(store, config, origin=at(17, 59, 5))  # book at 17:59:09.5
    witness_at(store, at(18, 0, 0))
    collect(store, config, origin=at(18, 4, 0))  # book at 18:04:04.5
    witness_at(store, at(18, 5, 10))
    assert select(store, SELECTION_V2).reasons == ("book_stale",)
    assert select(store, SELECTION_V1).reasons == ("book_availability_after_cutoff",)


def test_v2_never_searches_past_an_unusable_anchor(store, config):
    # The latest book witnessed by C failed; an older good book is not a fallback.
    to_schema4(store)
    collect(store, config, origin=at(18, 0, 0))
    good, _, _ = handler_for(fixture_data())

    def failing(request):
        if request.url.path.endswith("/orderbook"):
            return response(status=500, data={"error": "synthetic"})
        return good(request)

    collect(store, config, origin=at(18, 2, 0), handler=failing)
    witness_at(store, at(18, 4, 0))
    collect(store, config, origin=at(18, 4, 10))  # witnessed only after C
    witness_at(store, at(18, 5, 10))
    assert select(store, SELECTION_V2).reasons == ("book_fetch_failed",)


@pytest.mark.parametrize(
    "witnessed_at, reason",
    [(None, "book_availability_unproven"), (at(18, 5, 10), "book_availability_after_cutoff")],
)
def test_v2_reports_the_v1_reason_when_no_book_is_witnessed_by_cutoff(
    store, config, witnessed_at, reason
):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    if witnessed_at is not None:
        witness_at(store, witnessed_at)
    assert select(store, SELECTION_V2).reasons == (reason,)
    assert select(store, SELECTION_V1).reasons == (reason,)


def test_v2_treats_an_untrusted_receipt_as_no_proof(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 1, 0))  # book at 18:01:04.5
    witness_at(store, at(18, 3, 0))  # trusted
    collect(store, config, origin=at(18, 3, 30))  # book at 18:03:34.5
    # A regressed clock: observed before the high-water mark and before retrieval.
    witness_at(store, at(18, 2, 0))
    assert select(store, SELECTION_V1).reasons == ("book_clock_untrusted",)
    chosen = select(store, SELECTION_V2)
    assert chosen.eligible
    assert chosen.book.observed_at == at(18, 3, 0)


def test_v2_bounds_its_scan_and_then_reports_the_latest_reason(store, config, monkeypatch):
    from vader_intelligence.forecast import selection

    to_schema4(store)
    collect(store, config, origin=at(18, 0, 0))
    witness_at(store, at(18, 2, 0))
    collect(store, config, origin=at(18, 3, 0))  # never witnessed
    monkeypatch.setattr(selection, "V2_SCAN_LIMIT", 1)
    assert select(store, SELECTION_V2).reasons == ("book_availability_unproven",)
    monkeypatch.setattr(selection, "V2_SCAN_LIMIT", 2)
    assert select(store, SELECTION_V2).eligible


def test_unknown_selection_versions_are_refused(store, config):
    with pytest.raises(ValueError, match="selection_version must be one of"):
        Protocol(selection_version="selection-v3")
    to_schema4(store)
    with pytest.raises(ValueError, match="unknown selection version"):
        select(store, "v1")


def test_default_protocol_is_unchanged(tmp_path):
    # Existing cohorts keep their configuration hash: selection-v1 stays the default.
    assert Protocol().selection_version == SELECTION_V1
    assert Protocol().config_hash == (
        "7e859c311dab81d85c76738ef8e8edec4d51c8814aba84a1ac0eeee3c841783c"
    )
    path = tmp_path / "research.toml"
    path.write_text('[research]\nselection_version = "selection-v2"\n')
    loaded = Protocol.load(path)
    assert loaded.selection_version == SELECTION_V2
    assert loaded.config_hash != Protocol().config_hash


def test_witness_after_cutoff_is_never_used(store, config):
    # Point in time: whichever version, the chosen book was witnessed at or before C.
    cycles(store, config, V2)
    for version in (SELECTION_V1, SELECTION_V2):
        chosen = select(store, version)
        if chosen.book is not None:
            assert chosen.book.observed_at <= CUTOFF
            assert chosen.book.retrieved_at <= CUTOFF
