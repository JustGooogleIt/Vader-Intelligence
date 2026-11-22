"""Private-key providers for the live adapter. Key material goes only to `auth.Signer`.

A provider returns a parsed private-key object from `load_private_key()`, never bytes
or text, so callers other than the signer have nothing to log. The raw buffer is
overwritten once parsed (best effort: CPython may keep transient copies, and the
subprocess output of the Keychain provider is an immutable `bytes` object).

- `FileKeyProvider(path)`: an unencrypted PEM file (PKCS#8 Ed25519, or PKCS#1/PKCS#8
  RSA as Kalshi generates, facts §1 [V:KEYS]). The file must be a regular file (the
  final path component may not be a symlink), owned by the current user, with no
  group/other permission bits (mode 0600 or 0400), and at most 16 KiB.
- `KeychainKeyProvider(service, account)`: macOS `security find-generic-password -s
  SERVICE -a ACCOUNT -w` through an injectable runner; tests never run the binary.

Errors and reprs never contain key bytes or file contents; exceptions raised while
parsing are replaced (no chained cause or context that could carry data).

Assumption (UNVERIFIED; listed in docs/live-canary-runbook.md): `security ... -w`
prints the stored secret as text, or as hexadecimal when it contains non-printable
characters such as the newlines of a PEM file; both forms are accepted.
"""

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

MAX_KEY_BYTES = 16384
SECURITY_BINARY = "/usr/bin/security"
_PEM_MARKER = b"-----BEGIN "
_HEX = re.compile(rb"(?:[0-9A-Fa-f]{2})+\Z")
_KEYCHAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@:+-]{0,199}\Z")


class SecretError(Exception):
    """A key could not be provided. Messages never contain key material."""


class SecretProvider(Protocol):
    def load_private_key(self): ...

    def describe(self) -> str: ...


def _wipe(buffer):
    for i in range(len(buffer)):
        buffer[i] = 0


def parse_private_key(buffer):
    """Parse an unencrypted PEM private key held in a bytearray, then wipe the buffer.

    Only Ed25519 and RSA keys are accepted (facts §1 [V:KEYS])."""
    key, failure = None, None
    try:
        if _PEM_MARKER not in buffer:
            failure = "key material is not PEM encoded"
        else:
            try:
                key = serialization.load_pem_private_key(buffer, password=None)
            except Exception:  # replaced below without chaining: no data may leak
                failure = "key material could not be parsed as an unencrypted PEM private key"
    finally:
        _wipe(buffer)
    if failure is not None:
        raise SecretError(failure)
    if not isinstance(key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
        raise SecretError("unsupported key type (Ed25519 or RSA required)")
    return key


class FileKeyProvider:
    """Key from a PEM file readable only by its owner (mode 0600 or 0400)."""

    def __init__(self, path):
        self.path = Path(path)

    def __repr__(self):
        return f"FileKeyProvider(path={str(self.path)!r})"

    def describe(self):
        return f"file:{self.path}"

    def load_private_key(self):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            reason = os.strerror(exc.errno) if exc.errno else type(exc).__name__
            raise SecretError(f"key file cannot be opened: {reason}") from None
        try:
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if not stat.S_ISREG(info.st_mode):
                raise SecretError("key file is not a regular file")
            if mode & 0o077:
                raise SecretError(
                    f"key file mode {mode:04o} grants group/other access; chmod 600 is required"
                )
            if info.st_uid != os.getuid():
                raise SecretError("key file is not owned by the current user")
            if not 0 < info.st_size <= MAX_KEY_BYTES:
                raise SecretError("key file size is outside the accepted range")
            buffer = bytearray(info.st_size)
            with os.fdopen(os.dup(fd), "rb", buffering=0) as handle:
                count = handle.readinto(buffer)
            if count != info.st_size:
                _wipe(buffer)
                raise SecretError("key file changed while it was read")
        finally:
            os.close(fd)
        return parse_private_key(buffer)


def run_security(argv, timeout):
    """Default runner for `KeychainKeyProvider` (never used by tests)."""
    return subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin"},
    )


class KeychainKeyProvider:
    """Key stored as a macOS generic password (the PEM text as the secret).

    `runner(argv, timeout)` must return an object with `returncode` and `stdout` (bytes),
    like `subprocess.run(..., capture_output=True)`."""

    def __init__(self, service, account, *, runner=None, timeout=10):
        for name, value in (("service", service), ("account", account)):
            if not isinstance(value, str) or not _KEYCHAIN_NAME.fullmatch(value):
                raise SecretError(f"keychain {name} name is invalid")
        self.service, self.account = service, account
        self.timeout = timeout
        self._runner = runner or run_security

    def __repr__(self):
        return f"KeychainKeyProvider(service={self.service!r}, account={self.account!r})"

    def describe(self):
        return f"keychain:{self.service}/{self.account}"

    def argv(self):
        return [
            SECURITY_BINARY,
            "find-generic-password",
            "-s",
            self.service,
            "-a",
            self.account,
            "-w",
        ]

    def load_private_key(self):
        failed = False
        try:
            result = self._runner(self.argv(), self.timeout)
            returncode, stdout = result.returncode, result.stdout
        except Exception:  # replaced below without chaining
            failed = True
        if failed:
            raise SecretError("keychain lookup could not be run")
        if returncode != 0:
            raise SecretError(f"keychain item unavailable (security exit status {returncode})")
        if not isinstance(stdout, (bytes, bytearray)) or len(stdout) > 2 * MAX_KEY_BYTES + 2:
            raise SecretError("keychain output is not acceptable key material")
        buffer = bytearray(stdout)
        while buffer and buffer[-1] in b"\r\n":
            buffer.pop()
        if _PEM_MARKER not in buffer and _HEX.fullmatch(buffer):
            decoded = bytearray(bytes.fromhex(buffer.decode("ascii")))
            _wipe(buffer)
            buffer = decoded
        return parse_private_key(buffer)
