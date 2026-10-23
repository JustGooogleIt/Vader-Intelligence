"""Content-addressed, write-once artifact store with verification on every read.

Layout: `<root>/sha256/<first two hex>/<sha256>`. A file is published by hard-linking a
fully written, fsynced temporary file into place, so a path either does not exist or
holds complete bytes; an existing path is never replaced. Published files are made
read-only. Reads re-hash the bytes and refuse a mismatch.
"""

import hashlib
import io
import os
import tempfile
from pathlib import Path

from .contracts import HEX64

CHUNK = 1 << 20


class ArtifactCorrupted(ValueError):
    """Bytes do not hash to the expected digest (on write, on read, or already stored)."""


class ArtifactMissing(FileNotFoundError):
    pass


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        (self.root / "sha256").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    def path(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not HEX64.match(sha256):
            raise ValueError("artifact id must be a sha256 hex digest")
        return self.root / "sha256" / sha256[:2] / sha256

    def has(self, sha256: str) -> bool:
        return self.path(sha256).is_file()

    def put_bytes(self, data: bytes, expected_sha256: str | None = None) -> str:
        sha, _ = self.put_stream(io.BytesIO(data), expected_sha256=expected_sha256)
        return sha

    def put_stream(self, stream, *, expected_sha256=None, max_bytes=None) -> tuple[str, int]:
        """Copy a binary stream into the store; return (sha256, size).

        With `expected_sha256` the copy is refused (nothing published) on mismatch. An
        already-published digest is re-verified instead of being overwritten.
        """
        digest = hashlib.sha256()
        size = 0
        handle, temp = tempfile.mkstemp(dir=self.root / "tmp", prefix="put-")
        try:
            with os.fdopen(handle, "wb") as out:
                while True:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ArtifactCorrupted("artifact larger than declared")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = digest.hexdigest()
            if expected_sha256 is not None and sha != expected_sha256:
                raise ArtifactCorrupted(f"artifact hash mismatch: expected {expected_sha256}")
            target = self.path(sha)
            if target.exists():
                self.verify(sha)
                return sha, size
            target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(temp, 0o444)
            try:
                os.link(temp, target)
            except FileExistsError:
                self.verify(sha)
                return sha, size
            _fsync_dir(target.parent)
            return sha, size
        finally:
            os.unlink(temp)

    def get_bytes(self, sha256: str) -> bytes:
        """Read and verify; a corrupted file raises ArtifactCorrupted, never returns."""
        target = self.path(sha256)
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            raise ArtifactMissing(sha256) from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ArtifactCorrupted(f"stored artifact {sha256} fails verification")
        return data

    def verify(self, sha256: str) -> None:
        digest = hashlib.sha256()
        try:
            with open(self.path(sha256), "rb") as handle:
                while chunk := handle.read(CHUNK):
                    digest.update(chunk)
        except FileNotFoundError:
            raise ArtifactMissing(sha256) from None
        if digest.hexdigest() != sha256:
            raise ArtifactCorrupted(f"stored artifact {sha256} fails verification")
