"""Local configuration, durable metadata, and per-database advisory locks."""

import hashlib
import json
import os
import sqlite3
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


SHADOW_ROLE = "forward-shadow"
SHADOW_KEYS = {"role", "research_config", "tick_budget", "forbidden_paths"}
SHADOW_OPTIONAL = {"model_registry"}


def validate_shadow(config):
    """The optional isolated forward-shadow block (release milestone 5).

    A shadow configuration can only target isolated storage: it requires the test label,
    an existing research protocol file, a tick budget that fits the cadence together
    with the collection deadline, and an explicit list of forbidden production paths
    that none of its own paths may equal or live under.
    """
    shadow = config["shadow"]
    if (
        not isinstance(shadow, dict)
        or not SHADOW_KEYS <= set(shadow) <= SHADOW_KEYS | SHADOW_OPTIONAL
    ):
        raise ValueError(
            "shadow block must have exactly role, research_config, tick_budget, forbidden_paths"
            " (and optionally model_registry)"
        )
    if shadow["role"] != SHADOW_ROLE:
        raise ValueError(f"shadow role must be {SHADOW_ROLE!r}")
    if config["test_label"] is not True:
        raise ValueError("a forward-shadow configuration must use the test label")
    research = absolute(shadow["research_config"])
    if not research.is_file():
        raise ValueError("shadow research_config must be an existing absolute file path")
    budget = shadow["tick_budget"]
    if type(budget) is not int or not 1 <= budget <= 300:
        raise ValueError("tick_budget must be an integer in [1, 300]")
    if budget + config["deadline"] + 2 * config["grace"] >= config["cadence"]:
        raise ValueError(
            "tick_budget + deadline + two grace periods must be less than cadence"
            " (the combined wrapper budget, not the collection deadline alone)"
        )
    forbidden = shadow["forbidden_paths"]
    if not isinstance(forbidden, list) or not forbidden or len(forbidden) > 100:
        raise ValueError("forbidden_paths must list at least one absolute production path")
    resolved = [absolute(item).resolve() for item in forbidden]
    own = {
        "database": absolute(config["database"]).resolve(),
        "state": absolute(config["state"]).resolve(),
        "collector_config": absolute(config["collector_config"]).resolve(),
        "research_config": research.resolve(),
    }
    if "model_registry" in shadow:
        if not isinstance(shadow["model_registry"], str):
            raise ValueError("shadow model_registry must be an absolute directory path")
        # Existence and contents are checked when configuring (check_shadow_forecasters);
        # a lifecycle action (stop, uninstall) must still load the configuration if the
        # registry was removed later.
        own["model_registry"] = absolute(shadow["model_registry"]).resolve()
    for name, path in own.items():
        for banned in resolved:
            if _targets(path, banned):
                raise ValueError(
                    f"shadow {name} {path} targets a forbidden production path {banned}"
                )
    return shadow


def check_shadow_forecasters(shadow):
    """Configure-time check of a validated shadow block against its research protocol.

    When the protocol registers forecasters (D-004), the shadow needs a model registry
    from which every listed artifact opens read-only, exactly as the tick reads them.
    Without one, every tick fails before deciding and the shadow records no forward
    decisions at all, baselines included, while collection continues. Lifecycle loads
    never run this check, so a configuration whose registry was later removed can still
    be stopped and uninstalled.
    """
    from vader_intelligence.forecast.research import Protocol
    from vader_intelligence.research.registry import Registry

    protocol = Protocol.load(shadow["research_config"])
    if not protocol.forecasters:
        return
    if "model_registry" not in shadow:
        raise ValueError(
            "the shadow research protocol registers forecasters: model_registry is required"
        )
    root = absolute(shadow["model_registry"])
    if not root.is_dir():
        raise ValueError("shadow model_registry must be an existing directory")
    try:
        registry = Registry(root, readonly=True)
    except sqlite3.Error as exc:
        raise ValueError(f"shadow model_registry {root} is not a research registry") from exc
    try:
        for sha in protocol.forecasters:
            try:
                registry.model_artifact(sha)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"shadow model_registry cannot open forecaster {sha} read-only: {exc}"
                ) from exc
    finally:
        registry.close()


def _targets(path, banned):
    """True when `path` is `banned` or lies inside it: lexically, case-insensitively (the
    Mac's default volume ignores case), or by file identity of the path or any existing
    ancestor (which also defeats case changes and hard-link or symlink aliases)."""
    if path == banned or banned in path.parents:
        return True
    folded, banned_folded = str(path).casefold(), str(banned).casefold()
    if folded == banned_folded or folded.startswith(banned_folded.rstrip("/") + "/"):
        return True
    try:
        target = banned.stat()
    except OSError:
        return False
    for candidate in (path, *path.parents):
        try:
            info = candidate.stat()
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            return True
    return False


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
    if set(config) - {"shadow"} != expected or config["version"] != 1:
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
    if "shadow" in config:
        validate_shadow(config)
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


def tick_command(config):
    """The archive-only forecast tick that precedes collection in a shadow cycle."""
    shadow = config["shadow"]
    return [
        config["python"],
        "-m",
        "vader_intelligence.cli",
        "--config",
        config["collector_config"],
        "--db",
        config["database"],
        "forecast",
        "tick",
        "--research-config",
        shadow["research_config"],
        "--budget",
        str(shadow["tick_budget"]),
        # Registered forecasters (D-004) load their artifacts from the research registry.
        *(
            ["--model-registry", str(absolute(shadow["model_registry"]).resolve())]
            if "model_registry" in shadow
            else []
        ),
    ]


def maintenance_command(config, argv):
    """A bounded vader command run under the operations lock (settlement, research)."""
    return [
        config["python"],
        "-m",
        "vader_intelligence.cli",
        "--config",
        config["collector_config"],
        "--db",
        config["database"],
        *argv,
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
