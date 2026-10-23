"""End-to-end F3.4 slice through real application entry points (assignment §5).

Collection → discovery pass → witness → availability projection → tick selection →
policy/baseline → durable decision + publication → inspection → replay. Schema setup,
inspection and replay go through the real CLI (`cli.execute`); collection and the tick
use the library entry points with an injected transport and clock, the only way to
drive deterministic synthetic time. Nothing assembles the final records directly.
"""

import json

from _f34 import GAME_ID, at, collect, fixture_data, protocol, tick_at
from conftest import Clock, response
from test_collector import handler_for
from test_transport import reader_for

from vader_intelligence.cli import execute, parser
from vader_intelligence.collector import Collector
from vader_intelligence.config import Config
from vader_intelligence.storage import Store

PROTO = protocol(dataset="slice")
NS = PROTO.observer_namespace


def cli(path, *args):
    return execute(parser().parse_args(["--db", str(path), *args]))


def build_schema4_via_cli(path):
    assert cli(path, "db-init")["schema_version"] == 1
    assert cli(path, "settlement", "migrate")["schema_version"] == 2
    assert cli(path, "forecast", "migrate")["schema_version"] == 3
    rc2 = cli(path, "forecast", "migrate", "--record-contract", "3")
    assert rc2["schema_version"] == 5 and rc2["record_contract"] == 3


def test_vertical_slice_through_real_entry_points(tmp_path):
    path = tmp_path / "slice.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)
    build_schema4_via_cli(path)

    # Collection (library entry point, injected transport+clock) at C - 120 s.
    store = Store(path, min_free_bytes=1)
    try:
        result = collect(store, config, origin=at(18, 3))
        assert result["discovery_complete"] and result["books"] == 2
        # Warm-up tick witnesses the pass and prepares+witnesses the binding.
        warm = tick_at(store, PROTO, at(18, 3, 30))
        assert warm["status"] == "inconclusive" and warm["bindings_prepared"] == 1
        # Due tick decides within the publication window and publishes.
        due = tick_at(store, PROTO, at(18, 5, 30))
        assert due["decisions_eligible"] == 1 and due["publications"]["allowed"] == 1
    finally:
        store.close()

    # Inspection through the real CLI read-only path.
    report = cli(path, "forecast", "inspect", "--limit", "10")
    assert report["status"] == "complete" and report["count"] == 1
    (decision,) = report["decisions"]
    assert decision["game_id"] == GAME_ID and decision["cutoff_eligible"] is True
    assert decision["selected_ticker"].endswith("-HOU")
    assert decision["results"]["constant-v1"]["probability"] == "0.5"
    assert decision["results"]["midpoint-v1"]["probability"] == "0.6450"
    assert decision["publication"]["verdict"] == "allowed"
    assert decision["discovery"]["collector_config_hash"] != decision["config_hash"]

    # Replay through the real CLI: research rows preserved, decision unchanged, and the
    # projection still reproduces. Replay fetches nothing present-day.
    before = _decision_digest(path)
    replayed = cli(path, "replay")
    assert replayed["forecast_storage"]["replay"] == "preserved_not_reconstructed"
    counts = replayed["forecast_storage"]["record_contracts"]
    assert counts["forecast_decisions"] == {"1": 0, "2": 0, "3": 1, "4": 0}
    assert counts["forecast_bindings"]["1"] == 1
    assert _decision_digest(path) == before  # immutable across replay

    # Diagnostic recompute reproduces the stored result from the same persisted inputs.
    reopened = Store(path, min_free_bytes=1, readonly=True)
    try:
        from vader_intelligence.forecast import service

        recomputed = service.redecide(reopened.db, 1, NS)
        assert recomputed["stored_midpoint"] == "0.6450" and recomputed["midpoint"] == "0.6450"
        assert reopened.verify_integrity() == []
    finally:
        reopened.close()


def _decision_digest(path):
    db = Store(path, min_free_bytes=1, readonly=True)
    try:
        return db.db.execute("SELECT digest FROM forecast_decisions WHERE id=1").fetchone()[0]
    finally:
        db.close()


def test_slice_abstains_on_incomplete_discovery_end_to_end(tmp_path):
    path = tmp_path / "incomplete.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)
    build_schema4_via_cli(path)
    store = Store(path, min_free_bytes=1)
    try:

        def event_down(request):
            base, _, _ = handler_for(fixture_data())
            return response(503) if "/events/" in request.url.path else base(request)

        result = Collector(
            store, reader_for(store, config, event_down, _at(at(18, 3))), config
        ).run()
        assert result["discovery_complete"] is False
        tick_at(store, PROTO, at(18, 3, 30))
        due = tick_at(store, PROTO, at(18, 5, 30))
        assert due["decisions_eligible"] == 0 and due["bindings_prepared"] == 0
    finally:
        store.close()
    assert cli(path, "forecast", "inspect")["count"] == 0


def test_slice_empty_universe_is_inconclusive_not_failed(tmp_path):
    path = tmp_path / "empty.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)
    build_schema4_via_cli(path)
    store = Store(path, min_free_bytes=1)
    try:
        handler, _, _ = handler_for(fixture_data(), pages=lambda r: {"markets": [], "cursor": ""})
        result = Collector(store, reader_for(store, config, handler, _at(at(18, 3))), config).run()
        assert result["discovery_complete"] and result["eligible_contracts"] == 0
        tick_at(store, PROTO, at(18, 3, 30))
        due = tick_at(store, PROTO, at(18, 5, 30))
        assert due["status"] == "inconclusive" and due["errors"] == []
    finally:
        store.close()


def _at(moment):
    clock = Clock()
    clock.origin = moment
    return clock


def test_book_missing_abstains_midpoint_while_constant_stays_eligible(tmp_path):
    """A discovery-only run leaves no book; at C the constant is eligible on fresh,
    witnessed discovery while the midpoint abstains book_missing. Evidence available at
    C only: no later book can leak in."""
    path = tmp_path / "no-book.sqlite3"
    config = Config(database=str(path), min_free_bytes=1)
    build_schema4_via_cli(path)
    store = Store(path, min_free_bytes=1)
    try:
        handler, _, _ = handler_for(fixture_data())
        result = Collector(store, reader_for(store, config, handler, _at(at(18, 3))), config).run(
            kind="discover"
        )
        assert result["discovery_complete"] and result["books"] == 0
        tick_at(store, PROTO, at(18, 3, 30))
        due = tick_at(store, PROTO, at(18, 5, 30))
        assert due["decisions_eligible"] == 1  # constant still eligible
        decision = json.loads(
            store.db.execute("SELECT payload_json FROM forecast_decisions").fetchone()[0]
        )
        assert decision["results"]["constant-v1"]["probability"] == "0.5"
        assert decision["results"]["midpoint-v1"]["probability"] is None
        assert decision["results"]["midpoint-v1"]["reasons"] == ["book_missing"]
    finally:
        store.close()
