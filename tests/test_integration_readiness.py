"""Remaining mechanical integration findings; synthetic inputs and local files only."""

import hashlib
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ops.common import check_shadow_forecasters
from vader_intelligence.cli import execute, parser
from vader_intelligence.features import definitions
from vader_intelligence.forecast.forecasters import runner_from_registry
from vader_intelligence.forecast.research import Protocol
from vader_intelligence.research.registry import Registry
from vader_intelligence.trading.kalshi.activation import contains_key_material
from vader_intelligence.trading.ledger import Ledger
from vader_intelligence.trading.portfolio import CashEvent


def registry_artifact(root, feature_set):
    data = json.dumps(
        {
            "training": {"feature_set": feature_set},
            "feature_names": [d.name for d in definitions.feature_set(feature_set)],
            "family": "invalid-fixture",
        },
        sort_keys=True,
    ).encode()
    registry = Registry(root)
    sha = registry.put_artifact(data, ".json")
    registry.close()
    return sha


def test_configure_rejects_non_forward_artifact_before_writes(tmp_path):
    root = tmp_path / "registry"
    sha = registry_artifact(root, "fs-full-v1")
    research = tmp_path / "research.toml"
    research.write_text(f'[research]\ndataset="test"\nforecasters=["{sha}"]\n')
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="forward capable"):
        check_shadow_forecasters({"research_config": str(research), "model_registry": str(root)})
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("state", ["missing", "uninitialized"])
def test_removed_registry_becomes_an_explicit_unavailable_model(tmp_path, state):
    root = tmp_path / "registry"
    if state == "uninitialized":
        root.mkdir()
    sha = "a" * 64
    runner = runner_from_registry(root, Protocol(forecasters=[sha]))
    assert runner.models[sha]["data"] is None
    assert runner.models[sha]["error"]
    assert not (root / "research.sqlite3").exists()


def test_legacy_init_deposit_retry_preserves_cash_and_identity(tmp_path):
    path = tmp_path / "paper.sqlite3"
    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    ledger = Ledger(path, "paper")
    original = CashEvent("deposit-2026-10-01T00:00:00+00:00", "deposit", "1000", at, "init")
    ledger.append_cash_event(original)
    ledger.close()
    args = ["ledger", "init", "--ledger", str(path), "--environment", "paper", "--deposit"]
    execute(parser().parse_args(args + ["1000"]))
    ledger = Ledger(path, "paper", readonly=True)
    try:
        assert list(ledger.cash_events()) == [original]
    finally:
        ledger.close()
    with pytest.raises(ValueError, match="initial deposit"):
        execute(parser().parse_args(args + ["2000"]))
    ledger = Ledger(path, "paper", readonly=True)
    try:
        assert list(ledger.cash_events()) == [original]
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "encoding", [serialization.PrivateFormat.PKCS8, serialization.PrivateFormat.TraditionalOpenSSL]
)
def test_wrapped_private_key_body_without_its_header_is_refused(encoding):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    lines = key.private_bytes(
        serialization.Encoding.PEM, encoding, serialization.NoEncryption()
    ).splitlines()[1:-1]
    assert contains_key_material(b"\n".join(b"# " + line for line in lines[1:]))
    assert contains_key_material(b"\n".join(b"# " + line for line in lines[3:10]))


def test_key_detector_keeps_public_material_and_normal_configuration_usable():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    body = b"\n".join(b"# " + line for line in public.splitlines()[1:-1])
    assert not contains_key_material(body)
    digest = hashlib.sha256(b"synthetic config").hexdigest().encode()
    assert not contains_key_material(b"# artifact=" + digest + b'\npath="/Users/test/registry"\n')
