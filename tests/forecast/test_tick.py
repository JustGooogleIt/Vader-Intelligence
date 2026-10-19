"""The forecast tick orchestration and its specified behaviours (F3.4, spec §7, §2).

Synthetic throughout: collection uses an injected transport and clock, so every record
is synthetic lineage and ticks are run with injected clocks. The two-tick warm-up is
real: an earlier tick witnesses the binding and pass before the cutoff, a later tick
(within the publication window) decides and publishes.
"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from _f34 import GAME_ID, at, collect, fixture_data, protocol, tick_at, to_schema4
from conftest import Clock, response
from test_collector import handler_for
from test_transport import reader_for

from vader_intelligence.collector import Collector
from vader_intelligence.storage import Store, writer_lock

PROTO = protocol()
NS = PROTO.observer_namespace
WARMUP = at(18, 3, 30)  # after collection, before C
DUE = at(18, 5, 30)  # C + 30 s, inside the [C, C+150] publication window


def decisions(store):
    return [
        json.loads(r[0])
        for r in store.db.execute("SELECT payload_json FROM forecast_decisions ORDER BY id")
    ]


def test_full_tick_slice_eligible_decision_and_publication(store, config):
    to_schema4(store)
    result = collect(store, config, origin=at(18, 3))
    assert result["discovery_complete"] and result["books"] == 2
    warm = tick_at(store, PROTO, WARMUP)
    assert warm["status"] == "inconclusive"  # C not due yet
    assert warm["bindings_prepared"] == 1 and warm["due_opportunities"] == 0
    due = tick_at(store, PROTO, DUE)
    assert due["status"] == "complete"
    assert due["due_opportunities"] == 1 and due["decisions_eligible"] == 1
    assert due["decisions_abstained"] == 0 and due["publications"]["allowed"] == 1
    (decision,) = decisions(store)
    assert decision["cutoff_eligible"] is True and decision["game_id"] == GAME_ID
    assert decision["results"]["constant-v1"]["probability"] == "0.5"
    assert decision["results"]["midpoint-v1"]["probability"] == "0.6450"
    assert decision["contract_version"] == 2
    assert decision["manifest"]["discovery"]["collector_config_hash"] != decision["config_hash"]
    assert store.verify_integrity() == []


def test_repeated_due_tick_is_idempotent(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, WARMUP)
    first = tick_at(store, PROTO, DUE)
    before = (
        store.db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0],
        store.db.execute("SELECT COUNT(*) FROM forecast_publications").fetchone()[0],
    )
    second = tick_at(store, PROTO, at(18, 6, 0))
    after = (
        store.db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0],
        store.db.execute("SELECT COUNT(*) FROM forecast_publications").fetchone()[0],
    )
    assert first["decisions_eligible"] == 1
    assert second["due_opportunities"] == 1 and second["decisions_eligible"] == 0
    assert before == after == (1, 1)  # no duplicate decision or publication


def test_no_eligible_markets_is_a_recorded_abstention_not_an_error(store, config):
    to_schema4(store)
    handler, _, _ = handler_for(fixture_data(), no_games=True)  # candidates all unmapped
    result = Collector(
        store, reader_for(store, config, handler, _clock_at(at(18, 3))), config
    ).run()
    assert result["discovery_complete"] and result["eligible_contracts"] == 0
    warm = tick_at(store, PROTO, WARMUP)
    assert warm["bindings_prepared"] == 0 and warm["errors"] == []
    due = tick_at(store, PROTO, DUE)
    assert due["status"] == "inconclusive" and due["errors"] == []
    assert due["due_opportunities"] == 0 and due["decisions_eligible"] == 0
    assert decisions(store) == []


def test_incomplete_discovery_yields_no_binding_and_no_decision(store, config):
    to_schema4(store)

    def event_down(request):
        base, _, _ = handler_for(fixture_data())
        return response(503) if "/events/" in request.url.path else base(request)

    result = Collector(
        store, reader_for(store, config, event_down, _clock_at(at(18, 3))), config
    ).run()
    assert result["discovery_complete"] is False
    tick_at(store, PROTO, WARMUP)
    due = tick_at(store, PROTO, DUE)
    # The incomplete pass is never witnessed as usable, so no game is bindable.
    assert due["bindings_prepared"] == 0 and due["due_opportunities"] == 0
    assert decisions(store) == []


def test_late_tick_misses_publication_but_keeps_cutoff_facts(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, WARMUP)
    # Decide well after C+150: publication is missed, the decision is still eligible.
    late = tick_at(store, PROTO, at(18, 20, 0))
    assert late["decisions_eligible"] == 1 and late["publications"]["missed"] == 1
    (decision,) = decisions(store)
    assert decision["cutoff_eligible"] is True
    assert decision["results"]["constant-v1"]["probability"] == "0.5"
    verdict, status = store.db.execute(
        "SELECT json_extract(payload_json,'$.verdict'),"
        " json_extract(payload_json,'$.manifest.policy_status') FROM forecast_publications"
    ).fetchone()
    assert verdict == "missed" and status == "missed"


def test_restart_after_interruption_recovers_without_partial_forecast(store, config, monkeypatch):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, WARMUP)
    # Interrupt the due tick just before it persists the decision.
    import vader_intelligence.forecast.service as service_module

    real = service_module.decide_opportunity

    def boom(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(service_module, "decide_opportunity", boom)
    with pytest.raises(KeyboardInterrupt):
        tick_at(store, PROTO, DUE)
    assert decisions(store) == []  # nothing half-written
    monkeypatch.setattr(service_module, "decide_opportunity", real)
    recovered = tick_at(store, PROTO, at(18, 6, 0))
    assert recovered["decisions_eligible"] == 1
    assert len(decisions(store)) == 1 and store.verify_integrity() == []


def test_concurrent_due_ticks_do_not_duplicate(store, config, tmp_path):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, WARMUP)
    store.close()
    path = config.database

    def run_tick(_):
        from vader_intelligence.forecast.tick import tick as tick_fn

        clock = Clock()
        clock.origin = DUE
        try:
            with writer_lock(path):
                s = Store(path, min_free_bytes=1)
                try:
                    return tick_fn(s, PROTO, clock=clock.now, monotonic=clock.mono)["status"]
                finally:
                    s.close()
        except RuntimeError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run_tick, range(2)))
    # The writer lock serializes: at most one tick writes; the other reports busy or
    # runs after and finds the decision already present. Exactly one decision exists.
    reopened = Store(path, min_free_bytes=1)
    try:
        assert reopened.db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0] == 1
        assert reopened.db.execute("SELECT COUNT(*) FROM forecast_publications").fetchone()[0] == 1
    finally:
        reopened.close()
    assert "complete" in outcomes  # at least one tick ran to completion
    assert all(o in ("complete", "busy", "inconclusive") for o in outcomes)


def test_tick_requires_schema_four(store, config):
    from vader_intelligence.settlement.schema import migrate as settlement_migrate

    settlement_migrate(store)
    from vader_intelligence.forecast.schema import migrate as forecast_migrate

    forecast_migrate(store.db)  # schema 3 only
    with pytest.raises(ValueError, match="record contract 2"):
        tick_at(store, PROTO, DUE)


def test_inspect_explains_the_decision(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, WARMUP)
    tick_at(store, PROTO, DUE)
    from vader_intelligence.forecast.tick import inspect

    report = inspect(store, namespace=NS)
    assert report["count"] == 1
    (d,) = report["decisions"]
    assert d["game_id"] == GAME_ID and d["cutoff_eligible"] is True
    assert d["selected_ticker"].endswith("-HOU")
    assert d["results"]["constant-v1"]["probability"] == "0.5"
    assert d["publication"]["verdict"] == "allowed"
    assert d["discovery"]["collector_config_hash"] != d["config_hash"]
    with pytest.raises(ValueError, match="limit"):
        inspect(store, limit=200, namespace=NS)


def test_cohort_isolation_distinct_datasets_do_not_collide(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    other = protocol(dataset="second-cohort")
    # Run the cohorts interleaved in wall-clock order: both warm up, then both decide.
    # (Running one cohort fully ahead of the other would step the shared witness clock
    # backwards and correctly trip the regression guard.)
    tick_at(store, PROTO, WARMUP)
    _tick_proto(store, other, WARMUP)
    tick_at(store, PROTO, DUE)
    _tick_proto(store, other, DUE)
    rows = decisions(store)
    assert len(rows) == 2
    datasets = {r["dataset"] for r in rows}
    assert datasets == {"forward-shadow-test", "second-cohort"}
    collector_hashes = {r["manifest"]["discovery"]["collector_config_hash"] for r in rows}
    assert len(collector_hashes) == 1  # same collection, same collector hash
    research_hashes = {r["config_hash"] for r in rows}
    assert len(research_hashes) == 2  # distinct research cohorts


def _tick_proto(store, proto, moment):
    from vader_intelligence.forecast.tick import tick

    clock = Clock()
    clock.origin = moment
    return tick(store, proto, clock=clock.now, monotonic=clock.mono)


def _clock_at(moment):
    clock = Clock()
    clock.origin = moment
    return clock


def test_replay_after_appended_evidence_does_not_change_the_decision(store, config):
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, WARMUP)
    tick_at(store, PROTO, DUE)
    (before,) = decisions(store)
    digest_before = store.db.execute("SELECT digest FROM forecast_decisions WHERE id=1").fetchone()[
        0
    ]
    # A later collection appends a fresher book and pass; replay/redecide must not alter
    # the historical decision, which is frozen at its own cutoff.
    collect(store, config, origin=at(18, 10))
    tick_at(store, PROTO, at(18, 11, 0))
    assert store.db.execute("SELECT digest FROM forecast_decisions WHERE id=1").fetchone()[0] == (
        digest_before
    )
    from vader_intelligence.forecast import service

    recomputed = service.redecide(store.db, 1, NS)
    assert recomputed["stored_midpoint"] == before["results"]["midpoint-v1"]["probability"]
    assert recomputed["midpoint"] == recomputed["stored_midpoint"]
    assert store.verify_integrity() == []


def test_crash_between_decision_and_publication_leaves_not_attempted(store, config, monkeypatch):
    """Decision and publication are separate immutable records. A crash after the
    decision commits but before publication leaves the decision standing with no
    publication (not_attempted); it is not retroactively published by a later tick."""
    to_schema4(store)
    collect(store, config, origin=at(18, 3))
    tick_at(store, PROTO, WARMUP)
    import vader_intelligence.forecast.tick as tick_module

    real_publish = tick_module._publish

    def crash_publish(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(tick_module, "_publish", crash_publish)
    with pytest.raises(KeyboardInterrupt):
        tick_at(store, PROTO, DUE)
    # The decision is durable; the publication never committed.
    assert len(decisions(store)) == 1
    assert store.db.execute("SELECT COUNT(*) FROM forecast_publications").fetchone()[0] == 0
    monkeypatch.setattr(tick_module, "_publish", real_publish)
    # A later tick finds the decision present (idempotent) and does not republish it.
    later = tick_at(store, PROTO, at(18, 6, 30))
    assert later["decisions_eligible"] == 0 and later["due_opportunities"] == 1
    assert store.db.execute("SELECT COUNT(*) FROM forecast_publications").fetchone()[0] == 0
    assert store.verify_integrity() == []
