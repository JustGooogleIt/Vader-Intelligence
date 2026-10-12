"""The F3.3 vertical slice end to end (assignment §4), not isolated helpers.

Two-phase collection on schema 4 commits an immutable discovery summary and books; the
witness observes the committed pass and fetches at a later real time and appends
contract-2 receipts; the read-only projection turns those into the exact arguments the
merged F3.1 pure policy accepts; and policy.cutoff_eligibility plus the baselines
produce a decision for a cutoff after the observation and an abstention before it.

Everything is synthetic (injected transport and clock), so every receipt is synthetic
lineage, and the projection is read in that mode. No decision row is generated here:
decision/publication persistence is the later F3.3b remainder and F3.5 (plan §8.2).
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from conftest import Clock, response
from test_collector import handler_for
from test_transport import reader_for

from vader_intelligence.collector import Collector
from vader_intelligence.forecast import evidence, schema
from vader_intelligence.forecast.baselines import InvalidBook, constant, midpoint
from vader_intelligence.forecast.policy import cutoff_eligibility, utc
from vader_intelligence.forecast.witness import witness
from vader_intelligence.settlement.schema import migrate as settlement_migrate


def pass_receipt_observed(store, pass_id):
    return json.loads(
        store.db.execute(
            "SELECT payload_json FROM forecast_receipts WHERE discovery_id=?", (pass_id,)
        ).fetchone()[0]
    )["observed_at"]


def to_schema4(store):
    settlement_migrate(store)
    schema.migrate(store.db)
    schema.migrate(store.db, record_contract=2)


def common_eligibility(store, game_cutoff, namespace_mode="synthetic"):
    """Assemble the common cutoff eligibility exactly as a future tick would: the
    applicable discovery pass from the projection, plus per-fetch schedule and book
    availability, all through the pure policy. Returns (Eligibility, discovery)."""
    discovery = evidence.discovery_as_of(store.db, game_cutoff, mode=namespace_mode)
    # Schedule and market freshness use the discovery pass's own evidence times here;
    # a full selection step (later F3.3b) would witness the specific schedule/book rows.
    times = discovery.times
    eligibility = cutoff_eligibility(
        cutoff=game_cutoff,
        binding_observed_at=discovery.observed_at,
        discovery=times,
        schedule=times,
        market=times,
        universe_complete=discovery.universe_complete,
        identity_proven=True,
        pregame=True,
        contract_valid=True,
        clock_trusted=True,
    )
    return eligibility, discovery


def test_collect_witness_project_decide_end_to_end(store, config, fixture_data):
    to_schema4(store)
    clock = Clock()
    handler, _, _ = handler_for(fixture_data)
    result = Collector(store, reader_for(store, config, handler, clock), config).run()
    assert result["status"] == "complete" and result["books"] == 2
    pass_id = result["discovery_pass_id"]

    # The next tick runs one cycle later and witnesses the committed evidence.
    clock.value += 120
    summary = witness(store, clock=clock.now, monotonic=clock.mono)
    assert summary["status"] == "complete" and summary["passes_witnessed"] == 1
    observed_at = utc(datetime.fromisoformat(pass_receipt_observed(store, pass_id)))

    # A cutoff strictly after the observation and within 300 s: the universe is complete
    # and both baselines are eligible.
    cutoff = observed_at + timedelta(seconds=30)
    eligibility, discovery = common_eligibility(store, cutoff)
    assert discovery.kind == "pass" and discovery.pass_id == pass_id
    assert discovery.universe_complete is True
    assert eligibility.eligible, eligibility.reasons
    assert constant() == Decimal("0.5")

    # The one collected book normalizes to a midpoint through the merged F3.1 arithmetic.
    raw_book = json.loads(
        store.db.execute(
            "SELECT b.body FROM fetches f JOIN blobs b ON b.sha256=f.body_sha256 "
            "JOIN requests r ON r.id=f.request_id WHERE r.stage='book' AND f.state='ok' "
            "ORDER BY f.seq LIMIT 1"
        ).fetchone()[0]
    )
    probability = midpoint(raw_book)
    assert Decimal("0") <= probability <= Decimal("1")

    # A cutoff before the observation: availability is unproven, universe incomplete,
    # both baselines abstain. The same discovery evidence, only the clock differs.
    early = observed_at - timedelta(seconds=1)
    early_eligibility, early_discovery = common_eligibility(store, early)
    assert early_discovery.kind is None  # pass not yet witnessed at that cutoff
    assert not early_eligibility.eligible
    assert "universe_incomplete" in early_eligibility.reasons


def test_book_failure_leaves_the_slice_eligible_on_constant_only(store, config, fixture_data):
    """Case A2/G3 across the whole slice: a failed book never changes discovery
    eligibility; only the midpoint input is lost."""
    to_schema4(store)
    clock = Clock()
    handler, _, _ = handler_for(fixture_data)

    def book_times_out(request):
        if request.url.path.endswith("/orderbook"):
            clock.value += 60
            return response(data={"orderbook_fp": {}})
        return handler(request)

    result = Collector(store, reader_for(store, config, book_times_out, clock), config).run()
    assert result["status"] == "partial" and result["books"] == 0
    assert result["discovery_complete"] is True
    clock.value += 120
    witness(store, clock=clock.now, monotonic=clock.mono)
    observed = pass_receipt_observed(store, result["discovery_pass_id"])
    cutoff = utc(datetime.fromisoformat(observed)) + timedelta(seconds=30)
    eligibility, discovery = common_eligibility(store, cutoff)
    assert discovery.universe_complete is True and eligibility.eligible
    # No usable book was collected, so the midpoint baseline would abstain.
    assert (
        store.db.execute("SELECT COUNT(*) FROM observations WHERE kind='book'").fetchone()[0] == 0
    )


def test_incomplete_discovery_slice_abstains_and_collects_no_book(store, config, fixture_data):
    to_schema4(store)
    clock = Clock()
    handler, _, _ = handler_for(fixture_data)

    def event_down(request):
        return response(503) if "/events/" in request.url.path else handler(request)

    result = Collector(store, reader_for(store, config, event_down, clock), config).run()
    assert result["discovery_complete"] is False and result["books"] == 0
    clock.value += 120
    witness(store, clock=clock.now, monotonic=clock.mono)
    observed = pass_receipt_observed(store, result["discovery_pass_id"])
    cutoff = utc(datetime.fromisoformat(observed)) + timedelta(seconds=30)
    eligibility, discovery = common_eligibility(store, cutoff)
    assert discovery.kind == "pass" and discovery.universe_complete is False
    assert "event_unavailable" in discovery.reasons
    assert not eligibility.eligible and "universe_incomplete" in eligibility.reasons


def test_replay_preserves_the_slice_and_reports_contract_counts(store, config, fixture_data):
    to_schema4(store)
    clock = Clock()
    handler, _, _ = handler_for(fixture_data)
    result = Collector(store, reader_for(store, config, handler, clock), config).run()
    clock.value += 120
    witness(store, clock=clock.now, monotonic=clock.mono)
    from vader_intelligence.replay import replay

    before = store.db.execute("SELECT COUNT(*) FROM forecast_receipts").fetchone()[0]
    outcome = replay(store)
    assert outcome["forecast_storage"]["replay"] == "preserved_not_reconstructed"
    assert outcome["forecast_storage"]["record_contracts"]["forecast_receipts"] == {
        "1": 0,
        "2": before,
    }
    assert outcome["forecast_storage"]["latest_discovery"]["id"] == result["discovery_pass_id"]
    assert store.db.execute("SELECT COUNT(*) FROM forecast_receipts").fetchone()[0] == before
    assert store.verify_integrity() == []


@pytest.mark.parametrize("notional", ["1.0000", "2.0000"])
def test_midpoint_arithmetic_is_the_merged_f31_contract(notional):
    book = {"orderbook_fp": {"yes_dollars": [["0.4001", "3"]], "no_dollars": [["0.5799", "4"]]}}
    if notional == "2.0000":
        with pytest.raises(InvalidBook, match="unsupported_notional"):
            midpoint(book, notional)
    else:
        from decimal import Decimal

        assert midpoint(book, notional) == Decimal("0.4101")
