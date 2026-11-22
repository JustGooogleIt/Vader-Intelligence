"""Independent release-validation probes for the five integration repairs (F39, F47,
F48, F49, F51). Synthetic inputs, throwaway keys and disposable files only."""

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from test_collector import handler_for
from test_transport import reader_for

from ops.common import check_shadow_forecasters
from vader_intelligence.cli import execute, parser
from vader_intelligence.collector import Collector
from vader_intelligence.features import definitions
from vader_intelligence.forecast.forecasters import runner_from_registry
from vader_intelligence.forecast.research import Protocol
from vader_intelligence.forecast.schema import migrate as forecast_migrate
from vader_intelligence.research.registry import Registry
from vader_intelligence.settlement.schema import migrate as settlement_migrate
from vader_intelligence.trading.kalshi.activation import contains_key_material
from vader_intelligence.trading.ledger import Ledger
from vader_intelligence.trading.portfolio import CashEvent

AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def research(store):
    """Schema 3 through the two explicit migrations (as in test_discovery_boundary)."""
    settlement_migrate(store)
    forecast_migrate(store.db)
    return store


# --- F39 -----------------------------------------------------------------------------


def test_summary_runtime_error_keeps_raw_evidence_and_requests_no_books(
    research, config, fixture_data, monkeypatch
):
    handler, calls, _ = handler_for(fixture_data)

    def broken_append(table, payload):
        raise RuntimeError("insufficient free disk space")

    monkeypatch.setattr(research, "append_forecast", broken_append)
    before = research.db.execute("SELECT COUNT(*) FROM fetches").fetchone()[0]
    result = Collector(research, reader_for(research, config, handler), config).run()
    assert result["status"] == "failed"
    assert result["books_this_pass"] == 0
    assert any(e.startswith("discovery_summary_not_committed") for e in result["errors"])
    assert not any("orderbook" in str(c) for c in calls)
    status = research.db.execute("SELECT status FROM runs ORDER BY started_at DESC").fetchone()[0]
    assert status == "failed"
    assert research.db.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] > before


def test_summary_failure_record_that_cannot_be_written_propagates_without_books(
    research, config, fixture_data, monkeypatch
):
    """Documented limitation: when disk pressure also refuses the failure record, the
    error escapes and the run row stays `running`; still no book is requested."""
    handler, calls, _ = handler_for(fixture_data)

    def broken_append(table, payload):
        raise RuntimeError("insufficient free disk space")

    def broken_finish(run_id, status, result):
        raise RuntimeError("insufficient free disk space")

    monkeypatch.setattr(research, "append_forecast", broken_append)
    monkeypatch.setattr(research, "finish_run", broken_finish)
    with pytest.raises(RuntimeError):
        Collector(research, reader_for(research, config, handler), config).run()
    assert not any("orderbook" in str(c) for c in calls)
    assert research.db.execute("SELECT status FROM runs").fetchone()[0] == "running"


# --- F47 -----------------------------------------------------------------------------


def stub_artifact(feature_set, names):
    return json.dumps(
        {"family": "stub", "feature_names": names, "training": {"feature_set": feature_set}},
        sort_keys=True,
    ).encode()


NAMES = [d.name for d in definitions.feature_set("fs-market-v1")]
FULL = [d.name for d in definitions.feature_set("fs-full-v1")]


@pytest.mark.parametrize(
    "artifact,reason",
    [
        (b'{"synthetic": "model artifact"}', "training metadata"),
        (stub_artifact("fs-full-v1", FULL), "not forward capable"),
        (stub_artifact("fs-market-v1", NAMES[::-1]), "features differ"),
        (stub_artifact("fs-market-v1", NAMES), "cannot load"),
    ],
    ids=["unknown", "research-only", "features-differ", "unloadable"],
)
def test_configure_refuses_every_artifact_the_tick_would_refuse_or_abstain_on(
    tmp_path, artifact, reason
):
    """Port of the recovered wt-grok-h draft cases: configure builds the tick's runner."""
    root = tmp_path / "registry"
    registry = Registry(root)
    sha = registry.put_artifact(artifact, ".json")
    registry.close()
    protocol = tmp_path / "research.toml"
    protocol.write_text(f'[research]\ndataset="test"\nforecasters=["{sha}"]\n')
    before = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
    with pytest.raises(ValueError, match=reason):
        check_shadow_forecasters({"research_config": str(protocol), "model_registry": str(root)})
    assert sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file()) == before


# --- F48 -----------------------------------------------------------------------------


def legacy_ledger(path, amount, count=1):
    ledger = Ledger(path, "paper")
    events = []
    for i in range(count):
        event = CashEvent(f"deposit-2026-10-0{i + 1}T00:00:00+00:00", "deposit", amount, AT, "init")
        ledger.append_cash_event(event)
        events.append(event)
    ledger.close()
    return events


def cash_events(path):
    ledger = Ledger(path, "paper", readonly=True)
    try:
        return list(ledger.cash_events())
    finally:
        ledger.close()


def init(path, deposit):
    args = ["ledger", "init", "--ledger", str(path), "--environment", "paper", "--deposit"]
    return execute(parser().parse_args(args + [deposit]))


def test_an_equal_init_spelled_differently_is_the_same_deposit(tmp_path):
    path = tmp_path / "paper.sqlite3"
    original = legacy_ledger(path, "1000")
    init(path, "1000.00")  # Decimal equality, not text equality
    assert cash_events(path) == original


def test_ambiguous_duplicate_legacy_initializations_are_refused_without_cash(tmp_path):
    path = tmp_path / "paper.sqlite3"
    original = legacy_ledger(path, "1000", count=2)
    with pytest.raises(ValueError, match="initial deposit"):
        init(path, "1000")
    assert cash_events(path) == original


# --- F49 -----------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["corrupt-file", "wrong-schema"])
def test_an_unusable_registry_file_is_an_explicit_unavailable_model(tmp_path, state):
    root = tmp_path / "registry"
    root.mkdir()
    if state == "corrupt-file":
        (root / "research.sqlite3").write_bytes(b"not a database" * 100)
    else:
        db = sqlite3.connect(root / "research.sqlite3")
        db.execute("CREATE TABLE unrelated(x)")
        db.commit()
        db.close()
    sha = "b" * 64
    before = (root / "research.sqlite3").read_bytes()
    runner = runner_from_registry(root, Protocol(forecasters=[sha]))
    assert runner.models[sha]["data"] is None and runner.models[sha]["error"]
    assert (root / "research.sqlite3").read_bytes() == before


# --- F51 -----------------------------------------------------------------------------


def commented(body):
    return b"\n".join(b"# " + line for line in body.splitlines())


def test_headerless_ed25519_pkcs8_body_is_still_refused():
    key = ed25519.Ed25519PrivateKey.generate()
    lines = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).splitlines()[1:-1]
    assert contains_key_material(commented(b"\n".join(lines)))


def test_pkcs1_rsa_public_key_body_without_header_is_allowed():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.PKCS1
    )
    assert not contains_key_material(commented(b"\n".join(public.splitlines()[1:-1])))


def test_consecutive_hex_digests_are_not_a_wrapped_key_body():
    """False-positive probe: two bare SHA-256 digests on consecutive lines (a comment, or
    a TOML array) are hex, not a PEM-shaped base64 body."""
    digests = [hashlib.sha256(bytes([i])).hexdigest() for i in range(3)]
    array = (
        b"acknowledged = [\n" + b"".join(b'  "' + d.encode() + b'",\n' for d in digests) + b"]\n"
    )
    assert not contains_key_material(array)
    comment = b"# limits history\n# " + digests[0].encode() + b"\n# " + digests[1].upper().encode()
    assert not contains_key_material(comment)


def test_consecutive_absolute_paths_are_not_a_wrapped_key_body():
    paths = (
        b"/Users/operator/Documents/Vader-Intelligence-shadow/20261019-models/registry\n"
        b"/Users/operator/Documents/Vader-Intelligence-shadow/20261019-models/research\n"
    )
    assert all(60 <= len(line) <= 76 for line in paths.splitlines())
    assert not contains_key_material(paths)


def test_keyed_paths_and_ticker_arrays_are_not_key_material():
    toml = (
        b'enabled = true\nenvironment = "demo"\n[secret]\nprovider = "file"\n'
        b'path = "/Users/operator/Library/ApplicationSupport/vader/live-demo-keys/kalshi-demo"\n'
        b'[[markets]]\nseries = "KXMLBGAME"\ntickers = [\n'
        b'  "KXMLBGAME-26OCT192008CLETB-CLE",\n  "KXMLBGAME-26OCT192008CLETB-TB",\n]\n'
    )
    assert not contains_key_material(toml)


def test_wrapped_body_detector_runtime_is_bounded():
    worst = []
    for i in range(500):  # base64 lines that are neither hex nor paths
        worst.append(b"Qz+/" * 16)
        worst.append(b"zQ/+" * 16)
        worst.append(b"-")  # breaks the group: 500 separate two-line groups
    data = b"\n".join(worst)
    assert len(data) > 64 * 1024
    started = time.monotonic()
    assert contains_key_material(data)
    assert time.monotonic() - started < 2.0
