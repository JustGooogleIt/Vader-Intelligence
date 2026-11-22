"""Request signing for the Kalshi Trade API v2: RSA-PSS (SHA-256) and Ed25519 keys.

Verified facts relied on (facts §1, [V:KEYS][V:QAUTH][V:OAS securitySchemes]):

- every authenticated request carries `KALSHI-ACCESS-KEY` (the key id, a UUID),
  `KALSHI-ACCESS-TIMESTAMP` (milliseconds) and `KALSHI-ACCESS-SIGNATURE` (base64);
- the pre-sign text is `timestamp_ms + HTTP method + path`, the path taken from
  `/trade-api/v2` with no host and no query string;
- RSA keys sign with PSS, MGF1(SHA-256), SHA-256 and salt length = digest length;
  Ed25519 signs the UTF-8 message directly; the key type is read from the parsed key
  object (the PEM header does not identify it).

Assumptions (UNVERIFIED or undocumented; also listed in docs/live-canary-runbook.md):

- the request body is not part of the signed text (facts §1: [INF], implied by KEYS);
- the exchange's clock-skew tolerance is not documented (facts §1 "[gap]"). We refuse
  to sign while the most recent exchange `Date` header (one-second resolution) shows the
  local clock off by more than `max_skew_ms` (default 5000 ms). Before any `Date` header
  has been observed there is nothing to compare against and signing proceeds.

A `Signer` holds a parsed private-key object handed over by a `secrets` provider. It
never exposes the key: `repr` shows only the algorithm, pickling is refused, and errors
never contain key material.
"""

import base64
import logging
import re
import time
from email.utils import parsedate_to_datetime

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"
API_PREFIX = "/trade-api/v2"
METHODS = ("GET", "POST", "DELETE")
DEFAULT_MAX_SKEW_MS = 5000
DATE_RESOLUTION_MS = 1000
MIN_RSA_BITS = 2048
KEY_ID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z"
)
_SIGNED_PATH = re.compile(r"/trade-api/v2/[A-Za-z0-9_/-]{1,200}\Z")

log = logging.getLogger(__name__)


class SigningError(Exception):
    """Signing refused or impossible; nothing was sent."""


class ClockSkewError(SigningError):
    """The local clock disagrees with the exchange `Date` header beyond the bound."""


def wall_clock_ms():
    """Current wall-clock time in integer milliseconds since the Unix epoch."""
    return time.time_ns() // 1_000_000


def _validate(method, path):
    if method not in METHODS:
        raise SigningError(f"method {method!r} is not signable")
    if not isinstance(path, str) or not _SIGNED_PATH.fullmatch(path):
        raise SigningError("the signed path must start at /trade-api/v2 and exclude the query")


def presign_text(timestamp_ms, method, path):
    """The exact bytes that are signed: `timestamp_ms + METHOD + path` (facts §1)."""
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms < 0:
        raise SigningError("timestamp must be a nonnegative integer of milliseconds")
    _validate(method, path)
    return f"{timestamp_ms}{method}{path}".encode("utf-8")


class Signer:
    """Signs requests with one key. Construct with a key id and a private-key object
    from a `secrets` provider (or a throwaway key for dry runs and tests)."""

    __slots__ = ("key_id", "algorithm", "max_skew_ms", "_key", "_clock_ms", "_offset")

    def __init__(self, key_id, private_key, *, clock_ms=wall_clock_ms, max_skew_ms=None):
        if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
            raise SigningError("key id must be a UUID")
        if isinstance(private_key, ed25519.Ed25519PrivateKey):
            algorithm = "ed25519"
        elif isinstance(private_key, rsa.RSAPrivateKey):
            if private_key.key_size < MIN_RSA_BITS:
                raise SigningError(f"RSA keys must have at least {MIN_RSA_BITS} bits")
            algorithm = "rsa-pss-sha256"
        else:
            raise SigningError("unsupported key type (Ed25519 or RSA required)")
        skew = DEFAULT_MAX_SKEW_MS if max_skew_ms is None else max_skew_ms
        if isinstance(skew, bool) or not isinstance(skew, int) or skew <= 0:
            raise SigningError("max_skew_ms must be a positive integer")
        self.key_id = key_id
        self.algorithm = algorithm
        self.max_skew_ms = skew
        self._key = private_key
        self._clock_ms = clock_ms
        self._offset = None  # (lowest, highest) possible local - exchange offset, in ms

    @classmethod
    def from_provider(cls, key_id, provider, **options):
        """Signer whose key comes straight from `provider.load_private_key()`."""
        return cls(key_id, provider.load_private_key(), **options)

    def __repr__(self):
        return f"Signer(algorithm={self.algorithm!r})"

    def __reduce__(self):
        raise TypeError("a Signer cannot be pickled or copied")

    def clock_ms(self):
        value = self._clock_ms()
        if isinstance(value, bool) or not isinstance(value, int):
            raise SigningError("the clock must return integer milliseconds")
        return value

    # -- clock skew ----------------------------------------------------------------

    def observe_server_date(self, date_header, local_ms):
        """Record an exchange `Date` header received when the local clock read
        `local_ms`. Unparseable or missing headers are ignored (nothing observed)."""
        if not date_header:
            return
        try:
            server = parsedate_to_datetime(date_header)
        except (TypeError, ValueError, IndexError):
            return
        if server is None or server.tzinfo is None:
            return
        server_ms = int(server.timestamp()) * 1000
        # The exchange stamped some instant in [server_ms, server_ms + 999] at or before
        # the local reading, so local - exchange lies within these bounds.
        self._offset = (local_ms - server_ms - (DATE_RESOLUTION_MS - 1), local_ms - server_ms)

    @property
    def observed_offset_ms(self):
        """(lowest, highest) possible local-minus-exchange offset, or None."""
        return self._offset

    def check_clock(self):
        """Raise `ClockSkewError` when the last observation proves skew > bound."""
        if self._offset is None:
            return
        lowest, highest = self._offset
        if lowest > self.max_skew_ms or highest < -self.max_skew_ms:
            log.warning("signing refused: local clock skew exceeds %d ms", self.max_skew_ms)
            raise ClockSkewError(
                f"local clock differs from the exchange Date header by more than "
                f"{self.max_skew_ms} ms (observed offset {lowest}..{highest} ms)"
            )

    # -- signing ---------------------------------------------------------------------

    def sign(self, message):
        if self.algorithm == "ed25519":
            return self._key.sign(message)
        return self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def headers(self, method, path, *, check_clock=True):
        """The three authentication headers for one request (facts §1).

        With `check_clock` (order creation) a proven skew refuses signing; cancels and
        reads are signed regardless, so a skew observation can never stop a shutdown
        from cancelling (the exchange rejects a request it considers stale)."""
        _validate(method, path)
        if check_clock:
            self.check_clock()
        timestamp = self.clock_ms()
        signature = self.sign(presign_text(timestamp, method, path))
        return {
            HEADER_KEY: self.key_id,
            HEADER_TIMESTAMP: str(timestamp),
            HEADER_SIGNATURE: base64.b64encode(signature).decode("ascii"),
        }
