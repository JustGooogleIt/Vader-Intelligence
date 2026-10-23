"""Command Center configuration: where to read from and the pilot bounds."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 8765
REFRESH_SECONDS = 5
ACTIVITY_PAGE = 100
GLOBAL_AGENT_CAP = 4
ARCHIVE_BUSY_MS = 250
PREVIEW_BYTES = 64 * 1024
PANE_LINES = 60


@dataclass(frozen=True)
class CenterConfig:
    root: Path  # orchestration root (pods store, artifacts, Command Center store, tmux)
    archive: Path | None = None  # collection/forecast archive (read-only)
    ops_config: Path | None = None  # ops/manage.py configuration JSON (read-only)
    registry: Path | None = None  # research registry (read-only; shown, never passed to workers)
    observer_python: Path | None = None  # interpreter with the pinned tmux-jev, for pane capture
    repo: Path | None = None  # git checkout maintenance runs branch from (default: this package's)
    forbidden_paths: tuple = ()
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    refresh_seconds: int = REFRESH_SECONDS
    activity_page: int = ACTIVITY_PAGE
    global_agent_cap: int = GLOBAL_AGENT_CAP
    labels: dict = field(default_factory=dict)  # e.g. {"archive": "fixture"} shown in the UI

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).resolve())
        for name in ("archive", "ops_config", "registry", "observer_python", "repo"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).resolve())
        object.__setattr__(
            self, "forbidden_paths", tuple(Path(p).resolve() for p in self.forbidden_paths)
        )
        if self.host not in LOOPBACK_HOSTS:
            raise ValueError("the Command Center binds loopback only")
        if not 1024 <= self.port <= 65535:
            raise ValueError("port must be in 1024..65535")
        if not 1 <= self.refresh_seconds <= 60 or not 10 <= self.activity_page <= 100:
            raise ValueError("refresh_seconds in 1..60, activity_page in 10..100")
        if not 1 <= self.global_agent_cap <= 4:
            raise ValueError("global_agent_cap must be in 1..4 for this pilot")
        for name in ("archive", "ops_config", "registry"):
            value = getattr(self, name)
            if value is not None and self._forbidden(value):
                raise ValueError(f"{name} lies under a forbidden path")
        if self._forbidden(self.root):
            raise ValueError("root lies under a forbidden path")

    def _forbidden(self, path):
        return any(path == f or f in path.parents for f in self.forbidden_paths)

    @classmethod
    def load(cls, path):
        """A JSON file: {"root", "archive", "ops_config", "registry", "forbidden_paths",
        "host", "port", "labels"}; relative paths resolve against the file's directory."""
        path = Path(path).resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("command center config needs version 1")
        base = path.parent

        def absolute(value):
            return None if value is None else (base / os.path.expanduser(value)).resolve()

        return cls(
            root=absolute(data["root"]),
            archive=absolute(data.get("archive")),
            ops_config=absolute(data.get("ops_config")),
            registry=absolute(data.get("registry")),
            observer_python=absolute(data.get("observer_python")),
            repo=absolute(data.get("repo")),
            forbidden_paths=tuple(absolute(p) for p in data.get("forbidden_paths", ())),
            host=data.get("host", "127.0.0.1"),
            port=int(data.get("port", DEFAULT_PORT)),
            refresh_seconds=int(data.get("refresh_seconds", REFRESH_SECONDS)),
            labels=dict(data.get("labels", {})),
        )
