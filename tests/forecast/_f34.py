"""Shared builders for the F3.3b-remainder / F3.4 tests. All evidence is synthetic."""

import json
from datetime import datetime, timezone
from pathlib import Path

from conftest import Clock
from test_collector import handler_for
from test_transport import reader_for

from vader_intelligence.collector import Collector
from vader_intelligence.forecast.research import Protocol
from vader_intelligence.forecast.schema import migrate as forecast_migrate
from vader_intelligence.settlement.schema import migrate as settlement_migrate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/mlb.json"
# The shipped fixture's single game starts here; C = start - 3600.
GAME_START = datetime(2026, 9, 27, 19, 5, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 9, 27, 18, 5, tzinfo=timezone.utc)
GAME_ID = 824948


def to_schema4(store):
    settlement_migrate(store)
    forecast_migrate(store.db)
    forecast_migrate(store.db, record_contract=3)


def fixture_data():
    return json.loads(FIXTURE.read_text())


def at(hour, minute, second=0):
    return GAME_START.replace(hour=hour, minute=minute, second=second)


def collect(store, config, *, origin, handler=None, **handler_kwargs):
    """Two-phase collection on a schema-4 archive with an injected clock at `origin`."""
    clock = Clock()
    clock.origin = origin
    if handler is None:
        handler, _, _ = handler_for(fixture_data(), **handler_kwargs)
    result = Collector(store, reader_for(store, config, handler, clock), config).run()
    return result


def tick_at(store, protocol, moment, *, budget=30.0):
    """Run one library tick with an injected clock frozen at `moment` (monotonic 0)."""
    from vader_intelligence.forecast.tick import tick

    clock = Clock()
    clock.origin = moment
    return tick(store, protocol, clock=clock.now, monotonic=clock.mono, budget=budget)


def protocol(dataset="forward-shadow-test"):
    return Protocol(dataset=dataset)
