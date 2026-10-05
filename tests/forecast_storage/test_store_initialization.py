"""Native Store/CLI byte-preservation tests; no POSIX shims on Windows."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("Store/CLI require native fcntl; run on Mac", allow_module_level=True)

from vader_intelligence.storage import Store, writer_lock  # noqa: E402

from .fixtures import legacy, populated, snapshot  # noqa: E402
from .test_p2_regressions import old_layout  # noqa: E402


@pytest.fixture(params=["pre-fix-schema3", "future-schema"])
def unsupported(request, tmp_path):
    path = tmp_path / "unsupported.sqlite3"
    if request.param == "pre-fix-schema3":
        source, payloads = populated(":memory:")
        source.close()
        db = old_layout(path, payloads)
    else:
        db = legacy(path)
        db.execute("PRAGMA user_version=4")
    try:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        db.close()
    # Quiescent baseline: no live SQLite handle and no WAL/SHM/journal sidecars.
    assert list(tmp_path.glob(path.name + "*")) == [path]
    return path, path.read_bytes()


def assert_unchanged(path, original, *, lockfile=False):
    assert path.read_bytes() == original
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert path.read_bytes() == original
    expected = {path.name}
    if lockfile:
        expected.add(path.name + ".lockfile")
    assert {p.name for p in path.parent.glob(path.name + "*")} == expected


@pytest.mark.parametrize("readonly", [False, True])
def test_rejected_store_preserves_closed_delete_archive(unsupported, readonly):
    path, original = unsupported
    for _ in range(2):
        with pytest.raises((ValueError, RuntimeError), match="unsupported"):
            Store(path, min_free_bytes=1, readonly=readonly)
        assert_unchanged(path, original)


def invoke(path, *command):
    config = path.with_suffix(".toml")
    config.write_text("[collector]\nmin_free_bytes = 1\n")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "vader_intelligence.cli",
            "--config",
            str(config),
            "--db",
            str(path),
            *command,
        ],
        cwd=path.parent,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("command", [("db-init",), ("forecast", "migrate")])
def test_rejected_cli_preserves_closed_delete_archive(unsupported, command):
    path, original = unsupported
    for _ in range(2):
        result = invoke(path, *command)
        assert result.returncode == 1, (result.stdout, result.stderr)
        error = json.loads(result.stdout or result.stderr)["error"]
        assert ("unsupported" if command == ("db-init",) else "schema") in error
        # The persistent lock inode is intentional; no SQLite sidecars may remain.
        assert_unchanged(path, original, lockfile=True)
        with writer_lock(path):
            pass  # CLI released its lock even when Store construction failed.


@pytest.mark.parametrize("version", [0, 1, 2, 3])
def test_fresh_and_supported_initialization_still_work(tmp_path, version):
    path = tmp_path / "supported.sqlite3"
    if version:
        db = populated(path)[0] if version == 3 else legacy(path, version)
        before = snapshot(db)
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        db.close()
    for _ in range(2):
        store = Store(path, min_free_bytes=1)
        try:
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == (version or 1)
            assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            store.close()
    if version:
        with closing(sqlite3.connect(path)) as db:
            assert snapshot(db) == before


def test_cli_explicit_migrations_still_work(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    for command, version in (
        (("db-init",), 1),
        (("settlement", "migrate"), 2),
        (("db-init",), 2),
        (("forecast", "migrate"), 3),
        (("forecast", "migrate"), 3),
        (("db-init",), 3),
        (("settlement", "migrate"), 3),
    ):
        result = invoke(path, *command)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert json.loads(result.stdout)["schema_version"] == version


def test_validation_uses_the_actual_connection(unsupported, tmp_path, monkeypatch):
    path, original = unsupported
    approved_path = tmp_path / "approved.sqlite3"
    legacy(approved_path).close()
    real_connect = sqlite3.connect

    def open_replaced_target(target, *args, **kwargs):
        # Model a different target at open time. A separate path preflight is
        # insufficient; Store must validate the actual connection it will use.
        if Path(target) == approved_path:
            target = path
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", open_replaced_target)
    with pytest.raises((ValueError, RuntimeError), match="unsupported"):
        Store(approved_path, min_free_bytes=1)
    assert_unchanged(path, original)
