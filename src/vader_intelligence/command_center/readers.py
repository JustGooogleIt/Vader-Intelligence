"""Bounded, read-only access to the archive, the ops state and the orchestration store.

Every reader returns quickly or raises `Unavailable`; nothing here writes."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import ARCHIVE_BUSY_MS


class Unavailable(Exception):
    """The source cannot be read right now (missing, locked, malformed)."""


def now_iso(now=None):
    moment = datetime.now(timezone.utc) if now is None else now
    return moment.astimezone(timezone.utc).isoformat()


@contextmanager
def archive(path, busy_ms=ARCHIVE_BUSY_MS):
    """Read-only archive connection; a locked or missing file is `Unavailable`."""
    if path is None:
        raise Unavailable("no archive configured")
    path = Path(path)
    if not path.is_file():
        raise Unavailable("archive file missing")
    try:
        db = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=busy_ms / 1000,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise Unavailable(f"archive open failed: {type(exc).__name__}") from None
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
        db.execute("PRAGMA query_only=ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2, 3, 4, 5, 6):
            raise Unavailable(f"unsupported archive schema {version}")
        yield db
    except sqlite3.OperationalError as exc:
        text = str(exc)
        if "locked" in text or "busy" in text:
            raise Unavailable("archive busy") from None
        raise Unavailable(f"archive read failed: {text[:120]}") from None
    finally:
        db.close()


def schema_version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def has_table(db, name):
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def loads(text, default=None):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def ops_state(ops_config_path):
    """The ops configuration and its history state; `Unavailable` when unreadable."""
    if ops_config_path is None:
        raise Unavailable("no operations configuration")
    try:
        config = json.loads(Path(ops_config_path).read_text(encoding="utf-8"))
        history = json.loads((Path(config["state"]) / "history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Unavailable(f"operations state unreadable: {type(exc).__name__}") from None
    if not isinstance(history, dict) or history.get("version") != 1:
        raise Unavailable("operations state version unsupported")
    if history.get("database") != config.get("database"):
        raise Unavailable("operations state belongs to a different database")
    return config, history
