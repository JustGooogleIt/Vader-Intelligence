import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from vader_intelligence.config import Config
from vader_intelligence.provenance import RunProvenanceV1, source_hash
from vader_intelligence.storage import Store

FIXTURE = Path(__file__).parent / "fixtures/mlb.json"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in offline tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@pytest.fixture
def fixture_data():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def config(tmp_path):
    return Config(database=str(tmp_path / "vader.sqlite3"), min_free_bytes=1)


@pytest.fixture
def store(config):
    result = Store(config.database, min_free_bytes=1)
    yield result
    result.close()


@pytest.fixture
def run(store):
    store.start_run(RunProvenanceV1("test-run", "test", source_hash(), {}))
    return "test-run"


class Clock:
    def __init__(self):
        self.value = 0.0
        self.origin = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)

    def now(self):
        return self.origin + timedelta(seconds=self.value)

    def mono(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def response(status=200, data=None, body=None, headers=None):
    payload = body if body is not None else json.dumps(data or {}).encode()
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(payload))
