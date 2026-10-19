"""Live activation: the only constructor of a `KalshiGateway` with a network transport.

`activate_live(config_path, *, acknowledge_hash, ledger, now)` evaluates every gate of
docs/specs/live-adapter-v1.md at once and raises `ActivationRefused` listing ALL failed
gates; it returns a `KalshiGateway` only when every gate passes. It performs no network
request (the gateway connects lazily) and reads the credential only after every other
gate has passed. There is no automatic promotion from paper: the caller is an explicit
operator command (the CLI wiring, which must also require
`--i-understand-this-places-real-orders`, belongs to the integrator).

Gates (stable codes, evaluated in this order):

    config_parse            the file is a regular file (<= 64 KiB) of UTF-8 TOML
    config_schema           no unknown keys
    acknowledge_hash        operator's hash == SHA-256 of the exact file bytes
    enabled                 enabled = true (boolean)
    environment             "demo" or "production"
    key_id                  a key id reference (UUID); no key material in the file
    secret_provider         [secret] provider = "file" (absolute path) or "keychain"
                            (service, account)
    no_key_material         the file contains no private key: no PEM text, and no
                            base64 or hex run that decodes to PEM text or a DER
                            private key
    notional_caps           max_order_notional, max_daily_notional: positive decimal
                            strings, per-order <= daily
    markets                 [[markets]] allowed series/tickers with price grids
    clock                   `now` is timezone-aware
    ledger_live             the ledger environment is "live"
    ledger_consistent       ledger.verify() reports nothing
    risk_limits_complete    the ledger's current RiskLimits pass require_complete()
    risk_limits_hash        config risk_limits_hash == the ledger's current limits hash
    kill_switch_disengaged  latest kill-switch row is an explicit operator disengage
                            recorded at or before `now`
    secret_loads            (only when all the above pass) the provider yields a
                            supported private key

Config file (no key material; the key id is an identifier, not a secret):

    enabled = true
    environment = "demo"
    key_id = "<uuid>"
    risk_limits_hash = "<sha256 of the ledger's current RiskLimits>"
    max_order_notional = "2.00"
    max_daily_notional = "10.00"
    max_clock_skew_seconds = 5            # optional, 1..60

    [secret]
    provider = "file"                     # or "keychain" with service/account
    path = "/absolute/path/kalshi-demo.pem"

    [[markets]]
    series = "KXMLBGAME"
    exchange_index = 3                    # optional; MLB is on shard 3 (facts §12)
    tickers = []                          # optional allowlist inside the series
    price_ranges = [{ start = "0.0000", end = "1.0000", step = "0.0100" }]

(`KXMLBGAME` markets observed with `linear_cent` / one $0.01 range, facts §6 [V:L1];
`exchange_index: 3`, facts §12 [V:L2]. The grid must be re-checked per market before
activation: `price_ranges` is the source of truth.)
"""

import base64
import binascii
import hashlib
import hmac
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization

from ..money import dec, utc
from ..risk import LimitsIncomplete
from .auth import KEY_ID, Signer, SigningError
from .client import _NETWORK_TRANSPORT_KEY, KalshiClient
from .gateway import KalshiGateway, MarketRule
from .mapping import price_ranges
from .secrets import FileKeyProvider, KeychainKeyProvider, SecretError

GATES = (
    "config_parse",
    "config_schema",
    "acknowledge_hash",
    "enabled",
    "environment",
    "key_id",
    "secret_provider",
    "no_key_material",
    "notional_caps",
    "markets",
    "clock",
    "ledger_live",
    "ledger_consistent",
    "ledger_binding",
    "risk_limits_complete",
    "risk_limits_hash",
    "kill_switch_disengaged",
    "secret_loads",
)
ENVIRONMENTS = ("demo", "production")
MAX_CONFIG_BYTES = 65536
_TOP_KEYS = frozenset(
    {
        "enabled",
        "environment",
        "key_id",
        "risk_limits_hash",
        "max_order_notional",
        "max_daily_notional",
        "max_clock_skew_seconds",
        "secret",
        "markets",
    }
)
_SECRET_KEYS = frozenset({"provider", "path", "service", "account"})
_MARKET_KEYS = frozenset({"series", "exchange_index", "tickers", "price_ranges"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_KEY_MATERIAL = (b"-----BEGIN", b"PRIVATE KEY")
_BASE64_RUN = re.compile(rb"[A-Za-z0-9+/]{40,}={0,2}")
_HEX_RUN = re.compile(rb"[0-9A-Fa-f]{40,}")
_LAYOUT = re.compile(rb"[\s\"'\\]+")  # whitespace, quotes, TOML line continuations


def _decodes_to_key(blob):
    if any(marker in blob for marker in _KEY_MATERIAL):
        return True
    try:
        serialization.load_der_private_key(blob, password=None)
    except TypeError:  # an encrypted private key: still key material
        return True
    except Exception:  # not a DER private key
        return False
    return True


def contains_key_material(data):
    """True when `data` holds a private key in any form an operator might paste: PEM text,
    or a base64 or hex run (also across line breaks and quotes) that decodes to PEM text
    or a DER private key. A SHA-256 hex digest decodes to 32 bytes that are neither."""
    if any(marker in data for marker in _KEY_MATERIAL):
        return True
    for text in (data, _LAYOUT.sub(b"", data)):
        for match in _BASE64_RUN.finditer(text):
            run = match.group()
            try:
                blob = base64.b64decode(run + b"=" * (-len(run) % 4), validate=True)
            except (binascii.Error, ValueError):
                continue
            if _decodes_to_key(blob):
                return True
        for match in _HEX_RUN.finditer(text):
            run = match.group()
            if _decodes_to_key(bytes.fromhex(run[: len(run) // 2 * 2].decode())):
                return True
    return False


class ActivationRefused(Exception):
    """Live activation refused. `failed` lists every failed gate code."""

    def __init__(self, results):
        self.results = tuple(results)
        self.failed = tuple(r.gate for r in self.results if not r.passed)
        super().__init__("live activation refused; failed gates: " + ", ".join(self.failed))


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class LiveActivation:
    """A parsed activation config. Holds references only, never key material."""

    enabled: bool
    environment: str
    key_id: str
    secret: dict
    max_order_notional: object
    max_daily_notional: object
    risk_limits_hash: str
    markets: tuple
    max_clock_skew_seconds: int = 5

    def provider(self, *, keychain_runner=None):
        if self.secret["provider"] == "file":
            return FileKeyProvider(self.secret["path"])
        return KeychainKeyProvider(
            self.secret["service"], self.secret["account"], runner=keychain_runner
        )


def config_digest(data):
    """SHA-256 (hex) the operator acknowledges: the exact bytes of the config file."""
    return hashlib.sha256(data).hexdigest()


def _read(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("config file is missing or not a regular file")
    data = path.read_bytes()
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("config file is too large")
    return data


def _decimal_text(value, name):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal string")
    result = dec(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _secret(raw):
    if not isinstance(raw, dict) or set(raw) - _SECRET_KEYS:
        raise ValueError("[secret] must be a table of provider settings")
    provider = raw.get("provider")
    if provider == "file":
        path = raw.get("path")
        exact = set(raw) == {"provider", "path"}
        if not exact or not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("file provider needs exactly an absolute path")
        return {"provider": "file", "path": path}
    if provider == "keychain":
        if set(raw) != {"provider", "service", "account"}:
            raise ValueError("keychain provider needs exactly service and account")
        KeychainKeyProvider(raw["service"], raw["account"])  # validates the names
        return {"provider": "keychain", "service": raw["service"], "account": raw["account"]}
    raise ValueError("secret provider must be 'file' or 'keychain'")


def _markets(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError("at least one [[markets]] entry is required")
    rules = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - _MARKET_KEYS:
            raise ValueError("unknown keys in a [[markets]] entry")
        ranges = item.get("price_ranges")
        if not isinstance(ranges, list) or not all(
            isinstance(r, dict) and set(r) == {"start", "end", "step"} for r in ranges
        ):
            raise ValueError("price_ranges must list {start, end, step} tables")
        for entry in ranges:
            if not all(isinstance(v, str) for v in entry.values()):
                raise ValueError("price range bounds must be decimal strings")
        tickers = item.get("tickers", [])
        if not isinstance(tickers, list):
            raise ValueError("tickers must be a list")
        rules.append(
            MarketRule(
                series=item.get("series"),
                price_ranges=price_ranges(ranges),
                exchange_index=item.get("exchange_index"),
                tickers=tuple(tickers),
            )
        )
    if len({r.series for r in rules}) != len(rules):
        raise ValueError("a series may appear only once")
    return tuple(rules)


def check_gates(config_path, *, acknowledge_hash, ledger, now):
    """(gate results in GATES order without `secret_loads`, LiveActivation or None).

    Pure local checks: no network, no credential access."""
    results = {}

    def gate(name, check):
        try:
            value = check()
        except (ValueError, TypeError, KeyError, LimitsIncomplete) as exc:
            results[name] = GateResult(name, False, str(exc)[:300])
            return None
        results[name] = GateResult(name, True)
        return value

    data = None
    try:
        data = _read(config_path)
    except (OSError, ValueError) as exc:
        results["config_parse"] = GateResult("config_parse", False, str(exc)[:300])
    raw = None
    if data is not None:
        raw = gate("config_parse", lambda: tomllib.loads(data.decode("utf-8")))

        def acknowledged():
            expected = config_digest(data)
            given = acknowledge_hash.lower() if isinstance(acknowledge_hash, str) else ""
            if not _SHA256.fullmatch(given) or not hmac.compare_digest(given, expected):
                raise ValueError("acknowledge_hash does not match the SHA-256 of the config")

        gate("acknowledge_hash", acknowledged)

        def no_material():
            if contains_key_material(data):
                raise ValueError("the config file must not contain key material")

        gate("no_key_material", no_material)
    parsed = {}
    if raw is not None:

        def schema():
            unknown = sorted(set(raw) - _TOP_KEYS)
            if unknown:
                raise ValueError(f"unknown keys: {', '.join(unknown)}")

        gate("config_schema", schema)

        def enabled():
            if raw.get("enabled") is not True:
                raise ValueError("enabled must be true")
            return True

        parsed["enabled"] = gate("enabled", enabled)

        def environment():
            if raw.get("environment") not in ENVIRONMENTS:
                raise ValueError("environment must be 'demo' or 'production'")
            return raw["environment"]

        parsed["environment"] = gate("environment", environment)

        def key_id():
            value = raw.get("key_id")
            if not isinstance(value, str) or not KEY_ID.fullmatch(value):
                raise ValueError("key_id must be the API key id (a UUID)")
            return value

        parsed["key_id"] = gate("key_id", key_id)
        parsed["secret"] = gate("secret_provider", lambda: _secret(raw.get("secret")))

        def caps():
            order = _decimal_text(raw.get("max_order_notional"), "max_order_notional")
            daily = _decimal_text(raw.get("max_daily_notional"), "max_daily_notional")
            if order > daily:
                raise ValueError("max_order_notional exceeds max_daily_notional")
            skew = raw.get("max_clock_skew_seconds", 5)
            if isinstance(skew, bool) or not isinstance(skew, int) or not 1 <= skew <= 60:
                raise ValueError("max_clock_skew_seconds must be an integer in 1..60")
            return order, daily, skew

        parsed["caps"] = gate("notional_caps", caps)
        parsed["markets"] = gate("markets", lambda: _markets(raw.get("markets")))

    def clock():
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")
        return utc(now)

    moment = gate("clock", clock)

    def ledger_live():
        if ledger.environment != "live" or ledger.meta().get("environment") != "live":
            raise ValueError("the ledger environment must be live")

    gate("ledger_live", ledger_live)

    def consistent():
        failures = ledger.verify()
        if failures:
            raise ValueError(f"ledger verification failed: {failures[0]}")

    gate("ledger_consistent", consistent)

    def binding():
        """A live ledger trades one Kalshi environment with one key (bound at its first
        activation); reusing it elsewhere is refused."""
        meta = ledger.meta()
        for name, value in (
            ("kalshi_environment", parsed.get("environment")),
            ("kalshi_key_id", parsed.get("key_id")),
        ):
            if value is None:
                continue  # the environment/key_id gate already failed
            bound = meta.get(name)
            if bound is not None and bound != value:
                raise ValueError(f"the ledger is bound to {name}={bound!r}")

    gate("ledger_binding", binding)
    limits_hash = None

    def limits_complete():
        nonlocal limits_hash
        limits, limits_hash = ledger.current_limits()
        if limits is None:
            raise ValueError("no risk limits are recorded in the ledger")
        limits.require_complete()

    gate("risk_limits_complete", limits_complete)

    def limits_match():
        expected = raw.get("risk_limits_hash") if raw is not None else None
        if limits_hash is None or expected != limits_hash:
            raise ValueError("risk_limits_hash differs from the ledger's current limits")

    gate("risk_limits_hash", limits_match)

    def disengaged():
        state = ledger.kill_switch()
        if state.default or state.engaged:
            raise ValueError("the kill switch is not disengaged by an operator record")
        if moment is not None and state.at > moment:  # an invalid `now` fails `clock`
            raise ValueError("the disengage record is later than now")

    gate("kill_switch_disengaged", disengaged)

    unread = "not evaluated: the config could not be read"
    ordered = tuple(results.get(g, GateResult(g, False, unread)) for g in GATES[:-1])
    activation = None
    if all(r.passed for r in ordered):
        order_cap, daily_cap, skew = parsed["caps"]
        activation = LiveActivation(
            enabled=True,
            environment=parsed["environment"],
            key_id=parsed["key_id"],
            secret=parsed["secret"],
            max_order_notional=order_cap,
            max_daily_notional=daily_cap,
            risk_limits_hash=limits_hash,
            markets=parsed["markets"],
            max_clock_skew_seconds=skew,
        )
    return ordered, activation


def activate_live(
    config_path, *, acknowledge_hash, ledger, now, transport=None, keychain_runner=None
):
    """Return a live `KalshiGateway` when every gate passes; else `ActivationRefused`.

    `transport` and `keychain_runner` are test hooks: a supplied transport must be an
    `httpx.MockTransport` (the client refuses anything else); `None` creates the real
    network transport, the only place in the code base that does so.
    """
    results, activation = check_gates(
        config_path, acknowledge_hash=acknowledge_hash, ledger=ledger, now=now
    )
    if activation is None:
        raise ActivationRefused(results)
    try:
        signer = Signer.from_provider(
            activation.key_id,
            activation.provider(keychain_runner=keychain_runner),
            max_skew_ms=activation.max_clock_skew_seconds * 1000,
        )
    except (SecretError, SigningError) as exc:
        failed = GateResult("secret_loads", False, str(exc)[:300])
        raise ActivationRefused(results + (failed,)) from None
    ledger.bind_meta("kalshi_environment", activation.environment)
    ledger.bind_meta("kalshi_key_id", activation.key_id)
    if transport is None:
        # The trust store is httpx's bundled CA set, never SSL_CERT_FILE/SSL_CERT_DIR or
        # proxies from the environment: the order body is not signed (facts A2).
        context = httpx.create_ssl_context(trust_env=False)
        transport = httpx.HTTPTransport(retries=0, trust_env=False, verify=context)
        key = _NETWORK_TRANSPORT_KEY
    else:
        key = None
    client = KalshiClient(activation.environment, signer, transport=transport, network_key=key)
    return KalshiGateway(
        client,
        ledger=ledger,
        markets=activation.markets,
        max_order_notional=activation.max_order_notional,
        max_daily_notional=activation.max_daily_notional,
        fills_epoch=utc(ledger.meta()["created_at"]),
        risk_limits_hash=activation.risk_limits_hash,
    )
