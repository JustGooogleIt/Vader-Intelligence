"""Key providers: 0600 enforcement, no key bytes in exceptions/repr/logs, and the
Keychain provider through a stubbed runner (the real `security` binary never runs)."""

import logging
import os
import subprocess
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from vader_intelligence.trading.kalshi.auth import Signer
from vader_intelligence.trading.kalshi.secrets import (
    FileKeyProvider,
    KeychainKeyProvider,
    SecretError,
)

from ._kalshi_double import KEY_ID

SENTINEL = "SYNTHETICSECRETSENTINEL"


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the real security binary must never run in tests")

    monkeypatch.setattr(subprocess, "run", forbidden)


def pem(key, fmt=serialization.PrivateFormat.PKCS8, encryption=None):
    return key.private_bytes(
        serialization.Encoding.PEM, fmt, encryption or serialization.NoEncryption()
    )


def write_key(path, data, mode=0o600):
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def body_fragment(data):
    """A slice of the base64 body: must never appear in errors, reprs or logs."""
    return data.decode().splitlines()[1][:32]


def test_file_provider_loads_owner_only_keys(tmp_path):
    ed = ed25519.Ed25519PrivateKey.generate()
    r = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for name, data, mode in (
        ("ed.pem", pem(ed), 0o600),
        ("rsa.pem", pem(r, serialization.PrivateFormat.TraditionalOpenSSL), 0o400),
    ):
        provider = FileKeyProvider(write_key(tmp_path / name, data, mode))
        signer = Signer.from_provider(KEY_ID, provider)
        assert signer.algorithm in ("ed25519", "rsa-pss-sha256")
        assert body_fragment(data) not in repr(provider) + repr(signer)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o601])
def test_file_provider_refuses_group_or_other_access(tmp_path, mode):
    data = pem(ed25519.Ed25519PrivateKey.generate())
    provider = FileKeyProvider(write_key(tmp_path / "key.pem", data, mode))
    with pytest.raises(SecretError, match="chmod 600") as info:
        provider.load_private_key()
    assert body_fragment(data) not in str(info.value)


def test_file_provider_refuses_symlinks_directories_and_missing_files(tmp_path):
    data = pem(ed25519.Ed25519PrivateKey.generate())
    target = write_key(tmp_path / "real.pem", data)
    link = tmp_path / "link.pem"
    link.symlink_to(target)
    with pytest.raises(SecretError, match="cannot be opened"):
        FileKeyProvider(link).load_private_key()
    with pytest.raises(SecretError):
        FileKeyProvider(tmp_path).load_private_key()
    with pytest.raises(SecretError, match="cannot be opened"):
        FileKeyProvider(tmp_path / "missing.pem").load_private_key()
    empty = write_key(tmp_path / "empty.pem", b"")
    with pytest.raises(SecretError, match="size"):
        FileKeyProvider(empty).load_private_key()


def test_key_bytes_never_in_exceptions_reprs_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    good = pem(ed25519.Ed25519PrivateKey.generate())
    corrupted = good.replace(good.splitlines()[1], (SENTINEL * 3).encode()[:64])
    encrypted = pem(
        ed25519.Ed25519PrivateKey.generate(),
        encryption=serialization.BestAvailableEncryption(b"synthetic-password"),
    )
    unsupported = pem(ec.generate_private_key(ec.SECP256R1()))
    failures = []
    for name, data in (
        ("corrupted.pem", corrupted),
        ("encrypted.pem", encrypted),
        ("unsupported.pem", unsupported),
        ("plain.txt", SENTINEL.encode()),
    ):
        provider = FileKeyProvider(write_key(tmp_path / name, data))
        with pytest.raises(SecretError) as info:
            provider.load_private_key()
        failures.append(info.value)
        assert info.value.__cause__ is None and info.value.__context__ is None
    fragments = [SENTINEL, body_fragment(encrypted), body_fragment(unsupported)]
    text = " ".join(str(f) + repr(f) for f in failures) + caplog.text
    for fragment in fragments:
        assert fragment not in text


def stub_runner(stdout=b"", returncode=0, raises=None):
    calls = []

    def run(argv, timeout):
        calls.append((list(argv), timeout))
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    return run, calls


def test_keychain_provider_uses_find_generic_password_through_the_runner():
    data = pem(ed25519.Ed25519PrivateKey.generate())
    run, calls = stub_runner(data + b"\n")
    provider = KeychainKeyProvider("vader-kalshi-demo", "operator", runner=run)
    assert Signer.from_provider(KEY_ID, provider).algorithm == "ed25519"
    assert calls == [
        (
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "vader-kalshi-demo",
                "-a",
                "operator",
                "-w",
            ],
            10,
        )
    ]
    hex_run, _ = stub_runner(data.hex().encode() + b"\n")  # newlines make security print hex
    hex_provider = KeychainKeyProvider("vader-kalshi-demo", "operator", runner=hex_run)
    assert Signer.from_provider(KEY_ID, hex_provider).algorithm == "ed25519"
    assert body_fragment(data) not in repr(provider)


def test_keychain_provider_failures_carry_no_secret_material(caplog):
    caplog.set_level(logging.DEBUG)
    run, _ = stub_runner(SENTINEL.encode(), returncode=44)
    with pytest.raises(SecretError, match="exit status 44") as missing:
        KeychainKeyProvider("svc", "acct", runner=run).load_private_key()
    run, _ = stub_runner(raises=RuntimeError(SENTINEL))
    with pytest.raises(SecretError, match="could not be run") as broken:
        KeychainKeyProvider("svc", "acct", runner=run).load_private_key()
    assert broken.value.__context__ is None and broken.value.__cause__ is None
    run, _ = stub_runner(("-----BEGIN PRIVATE KEY-----\n" + SENTINEL + "\n").encode())
    with pytest.raises(SecretError) as garbage:
        KeychainKeyProvider("svc", "acct", runner=run).load_private_key()
    for error in (missing.value, broken.value, garbage.value):
        assert SENTINEL not in str(error) + repr(error)
    assert SENTINEL not in caplog.text
    for bad in ("-s", "", "x" * 300, "a\nb"):
        with pytest.raises(SecretError):
            KeychainKeyProvider(bad, "acct", runner=run)
