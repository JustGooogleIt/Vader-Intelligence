"""Signing: RSA-PSS and Ed25519 verified with the generated public key; the signed
path excludes the query; the timestamp is in milliseconds; clock-skew refusal.

Keys are generated inside the tests; nothing is sent anywhere (MockTransport only)."""

import base64
import pickle
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from vader_intelligence.trading.kalshi.auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    ClockSkewError,
    Signer,
    SigningError,
    presign_text,
)
from vader_intelligence.trading.kalshi.client import KalshiClient

from ._kalshi_double import KEY_ID, FakeClock, KalshiDouble

EXAMPLE_MS = 1703123456789  # the documented example timestamp (facts §1)


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pss(salt_length):
    return padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=salt_length)


def test_rsa_pss_signature_verifies_with_digest_length_salt(rsa_key):
    signer = Signer(KEY_ID, rsa_key, clock_ms=lambda: EXAMPLE_MS)
    headers = signer.headers("GET", "/trade-api/v2/portfolio/balance")
    assert headers[HEADER_KEY] == KEY_ID
    assert headers[HEADER_TIMESTAMP] == str(EXAMPLE_MS)
    message = b"1703123456789GET/trade-api/v2/portfolio/balance"  # facts §1 example
    assert presign_text(EXAMPLE_MS, "GET", "/trade-api/v2/portfolio/balance") == message
    signature = base64.b64decode(headers[HEADER_SIGNATURE], validate=True)
    rsa_key.public_key().verify(signature, message, pss(32), hashes.SHA256())
    with pytest.raises(InvalidSignature):  # any other salt length does not verify
        rsa_key.public_key().verify(signature, message, pss(64), hashes.SHA256())
    assert signer.algorithm == "rsa-pss-sha256"


def test_rsa_pkcs1_key_as_kalshi_generates_it(rsa_key):
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,  # BEGIN RSA PRIVATE KEY
        serialization.NoEncryption(),
    )
    assert pem.startswith(b"-----BEGIN RSA PRIVATE KEY-----")
    key = serialization.load_pem_private_key(pem, None)
    headers = Signer(KEY_ID, key, clock_ms=lambda: EXAMPLE_MS).headers(
        "POST", "/trade-api/v2/portfolio/events/orders"
    )
    rsa_key.public_key().verify(
        base64.b64decode(headers[HEADER_SIGNATURE]),
        b"1703123456789POST/trade-api/v2/portfolio/events/orders",
        pss(32),
        hashes.SHA256(),
    )


def test_ed25519_signature_verifies():
    key = ed25519.Ed25519PrivateKey.generate()
    signer = Signer(KEY_ID, key, clock_ms=lambda: EXAMPLE_MS)
    path = "/trade-api/v2/portfolio/events/orders/0000000a-0000-4000-8000-00000000000a"
    headers = signer.headers("DELETE", path)
    key.public_key().verify(
        base64.b64decode(headers[HEADER_SIGNATURE]), f"{EXAMPLE_MS}DELETE{path}".encode()
    )
    assert signer.algorithm == "ed25519"
    with pytest.raises(InvalidSignature):
        key.public_key().verify(
            base64.b64decode(headers[HEADER_SIGNATURE]), f"{EXAMPLE_MS}GET{path}".encode()
        )


def test_signed_path_excludes_query_and_host():
    key = ed25519.Ed25519PrivateKey.generate()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"orders": [], "cursor": ""})

    clock = FakeClock()
    client = KalshiClient(
        "demo",
        Signer(KEY_ID, key, clock_ms=clock.ms),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    client.request("GET", "/portfolio/orders", params={"ticker": "SYN-X-Y", "limit": 5})
    (request,) = seen
    assert request.url.host == "external-api.demo.kalshi.co"
    assert request.url.query == b"limit=5&ticker=SYN-X-Y"
    ts = request.headers[HEADER_TIMESTAMP]
    key.public_key().verify(
        base64.b64decode(request.headers[HEADER_SIGNATURE]),
        f"{ts}GET/trade-api/v2/portfolio/orders".encode(),
    )
    signer = Signer(KEY_ID, key)
    for bad in (
        "/trade-api/v2/portfolio/orders?limit=5",
        "/portfolio/orders",
        "https://external-api.kalshi.com/trade-api/v2/portfolio/orders",
        "/trade-api/v2/portfolio/orders#x",
    ):
        with pytest.raises(SigningError):
            signer.headers("GET", bad)
    with pytest.raises(SigningError):
        signer.headers("PUT", "/trade-api/v2/portfolio/orders")


def test_timestamp_is_integer_milliseconds():
    clock = FakeClock()
    signer = Signer(KEY_ID, ed25519.Ed25519PrivateKey.generate(), clock_ms=clock.ms)
    ts = signer.headers("GET", "/trade-api/v2/portfolio/balance")[HEADER_TIMESTAMP]
    assert len(ts) == 13 and int(ts) == clock.ms()
    assert int(ts) // 1000 == int(clock.now().timestamp())  # milliseconds, not seconds
    with pytest.raises(SigningError):
        Signer(KEY_ID, ed25519.Ed25519PrivateKey.generate(), clock_ms=lambda: 1.5e12).headers(
            "GET", "/trade-api/v2/portfolio/balance"
        )


def test_clock_skew_bound_uses_date_header_resolution():
    clock = FakeClock()
    signer = Signer(
        KEY_ID, ed25519.Ed25519PrivateKey.generate(), clock_ms=clock.ms, max_skew_ms=5000
    )
    path = "/trade-api/v2/portfolio/balance"
    signer.headers(path=path, method="GET")  # nothing observed yet: allowed

    def observe(offset_seconds, local_extra_ms=0):
        date = format_datetime(clock.now() + timedelta(seconds=offset_seconds), usegmt=True)
        signer.observe_server_date(date, clock.ms() + local_extra_ms)

    observe(-4)  # exchange 4 s behind: within the bound
    signer.headers("GET", path)
    observe(+5, local_extra_ms=999)  # could be just 5 s: not proven beyond the bound
    signer.headers("GET", path)
    observe(-7)  # local clock at least 6.001 s ahead
    with pytest.raises(ClockSkewError):
        signer.headers("GET", path)
    observe(+6)  # local clock at least 6 s behind
    with pytest.raises(ClockSkewError):
        signer.headers("GET", path)
    observe(0)  # fixed clock: signing resumes
    signer.headers("GET", path)
    signer.observe_server_date("not a date", clock.ms())  # ignored, last state kept
    signer.headers("GET", path)
    assert signer.observed_offset_ms is not None


def test_skew_refusal_through_the_client_sends_nothing_signed():
    clock = FakeClock()
    key = ed25519.Ed25519PrivateKey.generate()
    double = KalshiDouble(clock, key.public_key())
    double.date_offset = 30  # the exchange clock is 30 s ahead of ours
    client = KalshiClient(
        "demo",
        Signer(KEY_ID, key, clock_ms=clock.ms),
        transport=double.transport(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    client.request("GET", "/exchange/status")  # public: unsigned, observes the Date header
    with pytest.raises(ClockSkewError):
        client.request("GET", "/portfolio/balance")
    with pytest.raises(ClockSkewError):
        client.request(
            "POST",
            "/portfolio/events/orders",
            body={"ticker": "SYN-X-Y", "side": "bid", "count": "1.00", "price": "0.5000"},
        )
    assert [c[1] for c in double.calls] == ["/trade-api/v2/exchange/status"]


def test_signer_hides_key_and_refuses_unsupported_keys(rsa_key):
    signer = Signer(KEY_ID, rsa_key)
    der = rsa_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    assert repr(signer) == "Signer(algorithm='rsa-pss-sha256')"
    assert base64.b64encode(der).decode()[:40] not in repr(signer)
    with pytest.raises(TypeError):
        pickle.dumps(signer)
    with pytest.raises(SigningError):
        Signer(KEY_ID, ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(SigningError):
        Signer(KEY_ID, rsa.generate_private_key(public_exponent=65537, key_size=1024))
    with pytest.raises(SigningError):
        Signer("not-a-uuid", rsa_key)
    with pytest.raises(SigningError):
        Signer(KEY_ID, rsa_key, max_skew_ms=0)
