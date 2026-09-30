"""Local configuration, durable metadata, and per-database advisory locks."""

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"absolute path required: {value}")
    return path


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_json(path, value, *, new=False):
    """Publish complete metadata; new=True never replaces an existing name."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".ops-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if new:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path):
    """Never unlink the inode. Windows support permits isolated portable tests."""
    with open(path, "a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError("operations lock already held") from exc
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(config):
    expected = {
        "version",
        "checkout",
        "python",
        "database",
        "collector_config",
        "state",
        "cadence",
        "deadline",
        "grace",
        "retention",
        "log_bytes",
        "test_label",
    }
    if set(config) != expected or config["version"] != 1:
        raise ValueError("unsupported operations configuration")
    for key in ("checkout", "python", "database", "collector_config", "state"):
        absolute(config[key])
    for key, low, high in (
        ("cadence", 30, 86400),
        ("deadline", 1, 86400),
        ("grace", 1, 30),
        ("retention", 2, 1000),
        ("log_bytes", 1024, 4 * 1024 * 1024),
    ):
        value = config[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer in [{low}, {high}]")
    if config["deadline"] + 2 * config["grace"] >= config["cadence"]:
        raise ValueError("deadline + two cleanup grace periods must be less than cadence")
    if type(config["test_label"]) is not bool:
        raise ValueError("test_label must be boolean")
    db = absolute(config["database"])
    if str(db.resolve()) != str(db):
        raise ValueError("database must use its canonical absolute path")
    if db.exists() and db.stat().st_nlink != 1:
        raise ValueError("hard-linked databases are unsupported")
    state = absolute(config["state"])
    if db == state or state in db.parents:
        raise ValueError("research database must be outside the operations state directory")
    return config


def load(path, expected_hash=None):
    if expected_hash and digest(path) != expected_hash:
        raise ValueError("operations config changed: stop, uninstall and configure a new version")
    return validate(json.loads(Path(path).read_text(encoding="utf-8")))


def label(config):
    key = hashlib.sha256(config["database"].encode()).hexdigest()[:20]
    prefix = "com.vader.test.collection" if config["test_label"] else "com.vader.collection"
    return f"{prefix}.{key}"


def adjacent(config, suffix):
    return Path(config["database"] + ".ops-" + suffix)


def command(config):
    return [
        config["python"],
        "-m",
        "vader_intelligence.cli",
        "--config",
        config["collector_config"],
        "--db",
        config["database"],
        "collect",
        "--once",
    ]


def environment(config):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(config["checkout"]) / "src")
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONHOME", None)
    return env


def read_state(config):
    path = Path(config["state"]) / "history.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("database") != config["database"] or data.get("version") != 1:
        raise ValueError("operations state belongs to a different database or version")
    return data
