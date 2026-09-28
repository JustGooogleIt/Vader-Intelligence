"""Versioned storage boundary for collection and future research, not an agent runner."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def source_hash() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in {".py", ".sql"}:
            digest.update(str(p.relative_to(root)).encode())
            digest.update(b"\0" + p.read_bytes() + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class RunProvenanceV1:
    run_id: str
    kind: str
    code_hash: str
    configuration: dict[str, Any]
    schema_version: int = 1
    prompt_version: str | None = None
    model_provider: str | None = None
    model: str | None = None
    model_settings: dict[str, Any] | None = None
    input_snapshot_ids: list[str] = field(default_factory=list)
    tool_configuration: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    forecasts: list[str] = field(default_factory=list)
    abstentions: list[str] = field(default_factory=list)
    cost: dict[str, str] | None = None
    runtime_seconds: float | None = None
    evaluation_version: str | None = None
    outcome_ids: list[str] = field(default_factory=list)

    def to_json(self):
        return json_text(asdict(self))
