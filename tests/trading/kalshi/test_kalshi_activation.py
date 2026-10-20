"""Activation: every gate refused individually, acknowledge-hash mismatch, paper ledger
and incomplete limits refused, the credential read only after every other gate, and
only `activate_live` can create a network transport. No network request is made."""

import ast
import base64
import hashlib
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from _trading_synthetic import D, at
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

import vader_intelligence
from vader_intelligence.trading.kalshi.activation import (
    GATES,
    ActivationRefused,
    activate_live,
    check_gates,
    contains_key_material,
)
from vader_intelligence.trading.kalshi.client import LiveTransportRefused
from vader_intelligence.trading.kalshi.gateway import KalshiGateway
from vader_intelligence.trading.ledger import Ledger
from vader_intelligence.trading.risk import simulation_defaults

from ._kalshi_double import KEY_ID, FakeClock, KalshiDouble, live_ledger

NOW = at(0)
MARKETS = """
[[markets]]
series = "SYN"
exchange_index = 3
price_ranges = [{ start = "0.0000", end = "1.0000", step = "0.0100" }]
"""


def pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class Setup:
    def __init__(self, tmp_path, *, ledger=None, key_mode=0o600):
        self.tmp_path = tmp_path
        self.key = ed25519.Ed25519PrivateKey.generate()
        self.key_path = tmp_path / "kalshi-demo.pem"
        self.key_path.write_bytes(pem(self.key))
        os.chmod(self.key_path, key_mode)
        self.ledger = ledger or live_ledger(tmp_path / "live.sqlite3")
        self.config = tmp_path / "live.toml"

    def write(self, *, secret=None, markets=MARKETS, extra="", **overrides):
        _, limits_hash = self.ledger.current_limits()
        values = {
            "enabled": "true",
            "environment": '"demo"',
            "key_id": f'"{KEY_ID}"',
            "risk_limits_hash": f'"{limits_hash}"',
            "max_order_notional": '"50"',
            "max_daily_notional": '"200"',
            **overrides,
        }
        lines = [f"{k} = {v}" for k, v in values.items() if v is not None]
        secret = secret or f'[secret]\nprovider = "file"\npath = "{self.key_path}"\n'
        text = extra + "\n".join(lines) + "\n\n" + secret + markets
        self.config.write_text(text)
        return hashlib.sha256(text.encode()).hexdigest()

    def activate(self, ack=None, now=NOW, **hooks):
        ack = ack or hashlib.sha256(self.config.read_bytes()).hexdigest()
        hooks.setdefault("transport", httpx.MockTransport(self.forbid))
        return activate_live(
            self.config, acknowledge_hash=ack, ledger=self.ledger, now=now, **hooks
        )

    @staticmethod
    def forbid(request):
        raise AssertionError("activation must not send any request")

    def refused(self, **kwargs):
        with pytest.raises(ActivationRefused) as info:
            self.activate(**kwargs)
        return info.value


def test_all_gates_pass_and_return_a_live_gateway(tmp_path):
    setup = Setup(tmp_path)
    setup.write()
    clock = FakeClock()
    double = KalshiDouble(clock, setup.key.public_key())
    gateway = setup.activate(transport=double.transport())
    assert isinstance(gateway, KalshiGateway) and gateway.environment == "live"
    assert gateway.client.environment == "demo" and gateway.client.signer.algorithm == "ed25519"
    assert gateway.max_order_notional == D(50) and gateway.max_daily_notional == D(200)
    assert [m.series for m in gateway.markets] == ["SYN"] and gateway.markets[0].exchange_index == 3
    assert double.calls == []  # activation itself makes no request
    assert gateway.exchange_status()["trading_active"]  # public, unsigned
    results, activation = check_gates(
        setup.config,
        acknowledge_hash=hashlib.sha256(setup.config.read_bytes()).hexdigest(),
        ledger=setup.ledger,
        now=NOW,
    )
    assert [r.gate for r in results] == list(GATES[:-1]) and all(r.passed for r in results)
    assert activation.secret == {"provider": "file", "path": str(setup.key_path)}


def test_keychain_provider_through_a_stub_runner(tmp_path):
    setup = Setup(tmp_path)
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=pem(setup.key))

    setup.write(
        secret='[secret]\nprovider = "keychain"\nservice = "vader-kalshi"\naccount = "op"\n'
    )
    gateway = setup.activate(keychain_runner=runner)
    assert gateway.client.signer.algorithm == "ed25519"
    assert calls and calls[0][:2] == ["/usr/bin/security", "find-generic-password"]


def limits_without(name):
    return replace(simulation_defaults(D(1000)), **{name: None})


def paper_ledger(tmp_path):
    ledger = Ledger(tmp_path / "paper.sqlite3", "paper", created_at=at(-3600))
    ledger.record_limits(simulation_defaults(D(1000)), "test-operator", at(-3600))
    ledger.set_kill_switch(False, "operator: synthetic", "test-operator", at(-3600))
    return ledger


INDIVIDUAL = {
    "config_schema": lambda s: s.write(enable="true"),
    "enabled": lambda s: s.write(enabled="false"),
    "environment": lambda s: s.write(environment='"staging"'),
    "key_id": lambda s: s.write(key_id='"not-a-uuid"'),
    "secret_provider": lambda s: s.write(secret='[secret]\nprovider = "environment"\n'),
    "no_key_material": lambda s: s.write(extra="# -----BEGIN PRIVATE KEY-----\n"),
    "notional_caps": lambda s: s.write(max_order_notional='"300"'),
    "markets": lambda s: s.write(markets='[[markets]]\nseries = "SYN"\nprice_ranges = []\n'),
    "risk_limits_hash": lambda s: s.write(risk_limits_hash=f'"{"0" * 64}"'),
}


@pytest.mark.parametrize("gate", sorted(INDIVIDUAL))
def test_each_config_gate_is_refused_individually(tmp_path, gate):
    setup = Setup(tmp_path)
    INDIVIDUAL[gate](setup)
    assert setup.refused().failed == (gate,)


def test_float_caps_are_refused(tmp_path):
    setup = Setup(tmp_path)
    setup.write(max_order_notional="50.0")
    assert setup.refused().failed == ("notional_caps",)


def test_acknowledge_hash_mismatch(tmp_path):
    setup = Setup(tmp_path)
    good = setup.write()
    assert setup.refused(ack="f" * 64).failed == ("acknowledge_hash",)
    flipped = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert setup.refused(ack=flipped).failed == ("acknowledge_hash",)
    assert isinstance(setup.activate(ack=good.upper()), KalshiGateway)  # hex case-insensitive
    setup.config.write_text(setup.config.read_text() + "\n# edited after acknowledgement\n")
    assert setup.refused(ack=good).failed == ("acknowledge_hash",)


def test_unparseable_config(tmp_path):
    setup = Setup(tmp_path)
    setup.config.write_text("enabled = = true\n")
    error = setup.refused()
    assert "config_parse" in error.failed and "acknowledge_hash" not in error.failed
    assert not {"ledger_live", "ledger_consistent", "kill_switch_disengaged"} & set(error.failed)
    setup.config.unlink()
    with pytest.raises(ActivationRefused) as info:
        activate_live(setup.config, acknowledge_hash="0" * 64, ledger=setup.ledger, now=NOW)
    assert "config_parse" in info.value.failed


def test_clock_gate(tmp_path):
    setup = Setup(tmp_path)
    setup.write()
    assert setup.refused(now=datetime(2026, 10, 19, 22, 0)).failed == ("clock",)


def test_paper_ledger_refused(tmp_path):
    setup = Setup(tmp_path, ledger=paper_ledger(tmp_path))
    setup.write()
    assert setup.refused().failed == ("ledger_live",)


def test_incomplete_limits_refused(tmp_path):
    ledger = live_ledger(tmp_path / "live.sqlite3", limits=limits_without("max_open_orders"))
    setup = Setup(tmp_path, ledger=ledger)
    setup.write()
    error = setup.refused()
    assert error.failed == ("risk_limits_complete",)
    detail = {r.gate: r.detail for r in error.results}["risk_limits_complete"]
    assert "max_open_orders" in detail


def test_no_limits_recorded_refused(tmp_path):
    ledger = Ledger(tmp_path / "bare.sqlite3", "live", created_at=at(-3600))
    ledger.set_kill_switch(False, "operator: synthetic", "test-operator", at(-3600))
    setup = Setup(tmp_path, ledger=ledger)
    setup.write(risk_limits_hash='"none"')
    assert setup.refused().failed == ("risk_limits_complete", "risk_limits_hash")


def test_kill_switch_must_be_disengaged_by_an_operator_record(tmp_path):
    default = Setup(tmp_path, ledger=live_ledger(tmp_path / "a.sqlite3", disengage=False))
    default.write()
    assert default.refused().failed == ("kill_switch_disengaged",)  # live default: engaged
    later = Setup(tmp_path, ledger=live_ledger(tmp_path / "b.sqlite3"))
    later.write()
    later.ledger.set_kill_switch(True, "auto: daily_loss_stop", "risk-engine", at(-10))
    assert later.refused().failed == ("kill_switch_disengaged",)
    later.ledger.set_kill_switch(False, "operator reviewed", "test-operator", at(10))
    assert later.refused().failed == ("kill_switch_disengaged",)  # recorded after `now`
    assert isinstance(later.activate(now=at(11)), KalshiGateway)


def test_inconsistent_ledger_refused(tmp_path):
    setup = Setup(tmp_path)
    setup.write()
    setup.ledger.db.execute("DROP TRIGGER fills_environment")
    assert setup.refused().failed == ("ledger_consistent",)


def test_credential_read_only_after_every_other_gate(tmp_path):
    setup = Setup(tmp_path, key_mode=0o644)
    setup.write()
    error = setup.refused()
    assert error.failed == ("secret_loads",)
    assert "chmod 600" in {r.gate: r.detail for r in error.results}["secret_loads"]
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=pem(setup.key))

    setup.write(secret='[secret]\nprovider = "keychain"\nservice = "svc"\naccount = "op"\n')
    assert setup.refused(ack="0" * 64, keychain_runner=runner).failed == ("acknowledge_hash",)
    assert calls == []  # a refused activation never touches the credential


def test_only_activation_creates_a_network_transport(tmp_path):
    setup = Setup(tmp_path)
    setup.write()
    with pytest.raises(LiveTransportRefused):
        setup.activate(transport=httpx.HTTPTransport())
    real = setup.activate(transport=None)  # constructed, never used: no request is made
    assert type(real.client._http._transport) is httpx.HTTPTransport
    root = Path(vader_intelligence.__file__).parent
    users = {}
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            name = None
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
            elif isinstance(node, ast.Name) and node.id == "_NETWORK_TRANSPORT_KEY":
                name = node.id
            if name in ("HTTPTransport", "KalshiClient", "KalshiGateway", "_NETWORK_TRANSPORT_KEY"):
                users.setdefault(name, set()).add(path.relative_to(root).as_posix())
    activation = {"trading/kalshi/activation.py"}
    assert users["HTTPTransport"] == activation  # the only network transport constructor
    assert users["KalshiClient"] == activation
    assert users["KalshiGateway"] == activation
    assert users["_NETWORK_TRANSPORT_KEY"] == activation | {"trading/kalshi/client.py"}


def _pasted_forms():
    """Ways an operator might paste a private key into the activation file (keys are
    generated here and never leave the test)."""
    ser = serialization
    ed = ed25519.Ed25519PrivateKey.generate()
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = ed.private_bytes(ser.Encoding.DER, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    pkcs1 = rsa_key.private_bytes(
        ser.Encoding.DER, ser.PrivateFormat.TraditionalOpenSSL, ser.NoEncryption()
    )
    encrypted = ed.private_bytes(
        ser.Encoding.DER, ser.PrivateFormat.PKCS8, ser.BestAvailableEncryption(b"pw")
    )
    pem_text = pem(ed)
    body = b"".join(line for line in pem(rsa_key).splitlines() if b"-----" not in line)
    wrapped = b"\\\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    # Ed25519 PKCS#8 DER is 48 bytes, so its base64 is 64 characters with no padding:
    # split into lines shorter than a run, it merges with the next token of the file
    # ("enabled") once the layout is removed. The RSA body above does the same whenever
    # its DER length is a multiple of 3.
    short = base64.b64encode(der)
    assert not short.endswith(b"=")
    lines = body_lines(rsa_key)
    damaged = bytearray(b"".join(lines))
    damaged[len(damaged) // 2] = ord("A") if damaged[len(damaged) // 2] != ord("A") else ord("B")
    openssh = ed.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, ser.NoEncryption())
    return {
        "rsa_body_in_a_toml_array": b"[\n" + b",\n".join(b'"' + x + b'"' for x in lines) + b"]",
        "rsa_body_truncated": b"\n".join(lines[:12]),
        "rsa_body_one_character_damaged": bytes(damaged),
        "openssh_body": b"\n".join(x for x in openssh.splitlines() if b"-----" not in x),
        "ed25519_der_base64_unpadded_lines": short[:32] + b"\\\n" + short[32:],
        "ed25519_der_base64_after_a_character": b"x" + short,
        "ed25519_der_hex_after_a_digit": b"5" + der.hex().encode(),
        "ed25519_der_base64": base64.b64encode(der),
        "rsa_pkcs1_der_base64": base64.b64encode(pkcs1),
        "encrypted_der_base64": base64.b64encode(encrypted),
        "pem_body_wrapped": wrapped,  # armour removed, TOML line continuations
        "pem_base64": base64.b64encode(pem_text),
        "pem_hex": pem_text.hex().encode(),
        "der_hex": der.hex().encode(),
    }


def body_lines(key):
    """The base64 lines of a key's PEM without its armour."""
    return [line for line in pem(key).splitlines() if b"-----" not in line]


def test_a_body_pasted_as_comments_is_refused(tmp_path):
    # TOML ignores comments, so no other gate sees them: this gate is the only defence.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for case, prefix in enumerate(("# ", "#", "#\t")):
        comments = "".join(f"{prefix}{line.decode()}\n" for line in body_lines(key))
        directory = tmp_path / f"case{case}"
        directory.mkdir()
        setup = Setup(directory)
        setup.write(extra=comments)
        assert setup.refused().failed == ("no_key_material",)


def test_public_keys_are_not_key_material():
    for key in (
        ed25519.Ed25519PrivateKey.generate(),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    ):
        der = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        for blob in (base64.b64encode(der), der.hex().encode()):
            assert not contains_key_material(b'note = """\n' + blob + b'\n"""\n')


@pytest.mark.parametrize("form", sorted(_pasted_forms()))
def test_encoded_key_material_is_refused(tmp_path, form):
    blob = _pasted_forms()[form].decode()
    setup = Setup(tmp_path)
    setup.write(extra=f'note = """\n{blob}\n"""\n')
    assert "no_key_material" in setup.refused().failed


def test_hashes_and_paths_are_not_key_material():
    text = (
        f'risk_limits_hash = "{"ab" * 32}"\nkey_id = "{KEY_ID}"\n'
        'path = "/Users/operator/Documents/AbsolutelyLongDirectoryName/kalshi/key.pem"\n'
    ).encode()
    assert not contains_key_material(text)
