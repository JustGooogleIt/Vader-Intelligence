"""Typed contracts for pods, tasks, experiment manifests and worker result manifests.

Every external input (task submission, experiment manifest, worker result manifest) is
validated here with exact key sets, explicit types and bounded sizes before anything is
stored. Identities are content hashes of canonical JSON (`provenance.json_text`), so an
identical submission always maps to the same task id.
"""

import hashlib
import json
import math
import re
import types
import typing
from collections.abc import Mapping
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from typing import Any

from ..provenance import json_text

ROLES = ("baseline_models", "symbolic_search", "robustness_execution", "diagnostics")
TASK_STATES = (
    "queued",
    "leased",
    "retry_wait",
    "succeeded",
    "failed",
    "partial",
    "canceled",
    "quarantined",
)
TERMINAL_TASK_STATES = frozenset({"succeeded", "failed", "partial", "canceled", "quarantined"})
EVENT_KINDS = (
    "submitted",
    "leased",
    "heartbeat",
    "succeeded",
    "failed",
    "lost",
    "retry_scheduled",
    "cancel_requested",
    "canceled",
    "quarantined",
    "ingested",
)
ATTEMPT_OUTCOMES = ("succeeded", "partial", "failed", "lost", "canceled", "quarantined")
BUDGET_WINDOWS = {
    "max_concurrent": "instant",
    "cpu_seconds_per_day": "utc_day",
    "tasks_per_day": "utc_day",
    "wall_seconds_per_task": "per_task",
    "memory_mb": "per_task",
    "api_cost_usd": "utc_day",
}
ALLOWED_SPLITS = ("development", "validation")
EXPERIMENT_SCHEMA = "vi-experiment-manifest-v1"
RESULT_SCHEMA = "vi-pod-result-v1"
RESULT_STATUSES = ("succeeded", "partial", "failed")

MAX_RESULT_MANIFEST_BYTES = 1 << 20
MAX_OUTPUT_BYTES = 256 * 1024
MAX_ARTIFACTS = 64
MAX_PARAMS_BYTES = 64 * 1024

POD_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
OPERATION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ARTIFACT_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
ATTEMPT_ID = re.compile(r"^[0-9a-f]{32}-[1-9][0-9]{0,2}$")


class ContractError(ValueError):
    """An input does not satisfy its contract; never retried, never stored."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    """sha256 of canonical JSON; the identity function for every stored record."""
    return sha256_hex(json_text(value).encode())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def finite_number(value: Any) -> bool:
    """A non-bool int or float with a finite float value. A JSON integer too large for a
    float is not one: `math.isfinite` would raise OverflowError (reviews F42, F45)."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def integer(value: Any) -> bool:
    """A real integer, not a bool and not a float (review F43)."""
    return isinstance(value, int) and not isinstance(value, bool)


def strict_json(raw: bytes, limit: int, where: str) -> Any:
    """Decode bounded UTF-8 JSON; refuse NaN/Infinity and duplicate object keys."""
    require(isinstance(raw, bytes | bytearray), f"{where} must be bytes")
    require(len(raw) <= limit, f"{where} exceeds {limit} bytes")

    def pairs(items):
        keys = [key for key, _ in items]
        require(len(keys) == len(set(keys)), f"{where} has duplicate keys")
        return dict(items)

    def constant(name):
        raise ContractError(f"{where} contains non-finite number {name}")

    try:
        return json.loads(
            bytes(raw).decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"{where} is not valid JSON") from None


def _coerce(tp: Any, value: Any, where: str) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        options = typing.get_args(tp)
        if value is None and type(None) in options:
            return None
        concrete = [option for option in options if option is not type(None)]
        require(len(concrete) == 1, f"{where}: unsupported union annotation")
        return _coerce(concrete[0], value, where)
    if isinstance(tp, type) and is_dataclass(tp):
        if isinstance(value, tp):
            return value
        return from_mapping(tp, value, where)
    if tp is bool:
        require(isinstance(value, bool), f"{where} must be a boolean")
        return value
    if tp is int:
        require(isinstance(value, int) and not isinstance(value, bool), f"{where} must be an int")
        return value
    if tp is float:
        require(
            isinstance(value, int | float) and not isinstance(value, bool),
            f"{where} must be a number",
        )
        require(math.isfinite(value), f"{where} must be finite")
        return float(value)
    if tp is str:
        require(isinstance(value, str), f"{where} must be a string")
        return value
    if origin is tuple:
        args = typing.get_args(tp)
        require(len(args) == 2 and args[1] is Ellipsis, f"{where}: unsupported tuple annotation")
        require(isinstance(value, list | tuple), f"{where} must be an array")
        return tuple(_coerce(args[0], item, f"{where}[{i}]") for i, item in enumerate(value))
    if tp is dict or origin is dict:
        require(isinstance(value, Mapping), f"{where} must be an object")
        require(all(isinstance(key, str) for key in value), f"{where} keys must be strings")
        try:
            return json.loads(json_text(dict(value)))
        except (TypeError, ValueError):
            raise ContractError(f"{where} must be JSON-serializable") from None
    raise ContractError(f"{where}: unsupported field type {tp!r}")


def normalize_fields(instance: Any) -> None:
    """Check every field of a frozen contract dataclass against its annotation, in place.

    Called first in each contract's __post_init__, so direct construction and
    `from_mapping` produce identical, canonical values (ints widened to declared floats,
    arrays to tuples, objects to plain JSON dicts).
    """
    cls = type(instance)
    hints = typing.get_type_hints(cls)
    for item in fields(instance):
        value = _coerce(
            hints[item.name], getattr(instance, item.name), f"{cls.__name__}.{item.name}"
        )
        object.__setattr__(instance, item.name, value)


def from_mapping(cls: type, data: Any, where: str):
    """Build a contract dataclass from a mapping with exact keys and checked field types.

    Unknown keys and missing required keys are refused; booleans are never ints.
    """
    require(is_dataclass(cls), f"{where}: contract type must be a dataclass")
    require(isinstance(data, Mapping), f"{where} must be an object")
    hints = typing.get_type_hints(cls)
    names = [f.name for f in fields(cls) if f.init]
    unknown = sorted(str(key) for key in set(data) - set(names))
    require(not unknown, f"{where}: unknown keys {unknown}")
    required = [
        f.name
        for f in fields(cls)
        if f.init and f.default is MISSING and f.default_factory is MISSING
    ]
    missing = sorted(set(required) - set(data))
    require(not missing, f"{where}: missing keys {missing}")
    values = {key: _coerce(hints[key], data[key], f"{where}.{key}") for key in data}
    try:
        return cls(**values)
    except ContractError:
        raise
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ContractError(f"{where}: {exc}") from None


def to_plain(instance: Any) -> dict:
    """Canonical JSON-ready dict of a contract dataclass (tuples become lists)."""
    return json.loads(json_text(asdict(instance)))


def _match(value: str, pattern: re.Pattern, what: str) -> None:
    require(bool(pattern.match(value)), f"{what} must match {pattern.pattern}")


@dataclass(frozen=True)
class Budgets:
    """Per-pod limits. `api_cost_usd` must be 0: this release makes no paid calls."""

    max_concurrent: int = 1
    cpu_seconds_per_day: float = 3600.0
    tasks_per_day: int = 100
    wall_seconds_per_task: float = 600.0
    memory_mb: int = 2048
    api_cost_usd: float = 0.0

    def __post_init__(self):
        normalize_fields(self)
        require(1 <= self.max_concurrent <= 64, "max_concurrent must be in 1..64")
        require(0 < self.cpu_seconds_per_day <= 86400 * 64, "cpu_seconds_per_day out of range")
        require(1 <= self.tasks_per_day <= 100000, "tasks_per_day must be in 1..100000")
        require(0 < self.wall_seconds_per_task <= 86400, "wall_seconds_per_task out of range")
        require(64 <= self.memory_mb <= 1 << 20, "memory_mb out of range")
        require(self.api_cost_usd == 0.0, "api_cost_usd must be 0 (paid calls are disabled)")


@dataclass(frozen=True)
class PodSpec:
    pod_id: str
    role: str
    budgets: Budgets = field(default_factory=Budgets)

    def __post_init__(self):
        normalize_fields(self)
        _match(self.pod_id, POD_ID, "pod_id")
        require(self.role in ROLES, f"unknown pod role {self.role!r}")

    def config(self) -> dict:
        return {"role": self.role, "budgets": to_plain(self.budgets)}


@dataclass(frozen=True)
class TaskSpec:
    """A request to run one approved operation; `params` are checked by the registry."""

    pod_id: str
    type: str
    params: dict[str, Any]
    experiment_manifest_hash: str
    priority: int = 0
    max_attempts: int = 3

    def __post_init__(self):
        normalize_fields(self)
        _match(self.pod_id, POD_ID, "pod_id")
        _match(self.type, OPERATION_NAME, "task type")
        _match(self.experiment_manifest_hash, HEX64, "experiment_manifest_hash")
        require(len(json_text(self.params)) <= MAX_PARAMS_BYTES, "params too large")
        require(-1000 <= self.priority <= 1000, "priority must be in -1000..1000")
        require(1 <= self.max_attempts <= 10, "max_attempts must be in 1..10")


def task_identity(task_type: str, params: dict, experiment_manifest_hash: str) -> str:
    """Deterministic task id: sha256(type, normalized params, manifest hash)[:32]."""
    return content_hash(
        {"type": task_type, "params": params, "experiment_manifest_hash": experiment_manifest_hash}
    )[:32]


def _holdout_free(value: Any, where: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            require("holdout" not in str(key).lower(), f"{where} must not reference a holdout")
            _holdout_free(item, f"{where}.{key}")
    elif isinstance(value, list | tuple):
        for item in value:
            _holdout_free(item, where)


@dataclass(frozen=True)
class ExperimentManifest:
    """Immutable experiment description stored as an artifact before any task cites it.

    The split names only development/validation segments and no key anywhere may name a
    holdout: the final holdout is never part of a pod experiment.
    """

    dataset_manifest_hash: str
    feature_set_versions: dict[str, Any]
    split: dict[str, Any]
    code_hash: str
    operation: str
    params: dict[str, Any]
    seed: int
    budget: dict[str, Any]
    schema: str = EXPERIMENT_SCHEMA

    def __post_init__(self):
        normalize_fields(self)
        require(self.schema == EXPERIMENT_SCHEMA, "unsupported experiment manifest schema")
        _match(self.dataset_manifest_hash, HEX64, "dataset_manifest_hash")
        _match(self.code_hash, HEX64, "code_hash")
        _match(self.operation, OPERATION_NAME, "operation")
        require(bool(self.split), "split must define at least one segment")
        unknown = sorted(set(self.split) - set(ALLOWED_SPLITS))
        require(not unknown, f"split may only name {list(ALLOWED_SPLITS)}, got {unknown}")
        require(0 <= self.seed < 2**63, "seed must be a non-negative int64")
        for name in ("feature_set_versions", "split", "params", "budget"):
            _holdout_free(getattr(self, name), name)

    @classmethod
    def from_json(cls, raw: bytes) -> "ExperimentManifest":
        return from_mapping(cls, strict_json(raw, 4 * MAX_PARAMS_BYTES, "manifest"), "manifest")

    def to_bytes(self) -> bytes:
        return json_text(to_plain(self)).encode()


@dataclass(frozen=True)
class ArtifactRef:
    name: str
    kind: str
    sha256: str
    bytes: int

    def __post_init__(self):
        normalize_fields(self)
        _match(self.name, ARTIFACT_NAME, "artifact name")
        _match(self.kind, ARTIFACT_KIND, "artifact kind")
        _match(self.sha256, HEX64, "artifact sha256")
        require(0 <= self.bytes < 1 << 40, "artifact size out of range")


@dataclass(frozen=True)
class ErrorInfo:
    type: str
    message: str
    retryable: bool

    def __post_init__(self):
        normalize_fields(self)
        require(0 < len(self.type) <= 128, "error.type must be 1..128 characters")
        require(len(self.message) <= 2000, "error.message too long")


@dataclass(frozen=True)
class WorkerInfo:
    """What the worker observed about itself; audit evidence, excluded from result hashes."""

    pid: int
    env_names: tuple[str, ...]
    rlimits: dict[str, Any]
    cpu_seconds: float

    def __post_init__(self):
        normalize_fields(self)
        require(self.pid > 0, "worker.pid must be positive")
        require(len(self.env_names) <= 256, "too many environment names")
        require(self.cpu_seconds >= 0, "worker.cpu_seconds must be non-negative")


@dataclass(frozen=True)
class ResultManifest:
    """The only channel from a worker back to the coordinator (`result.json`)."""

    schema: str
    task_id: str
    attempt_id: str
    lease_token: str
    status: str
    output: dict[str, Any] | None
    error: ErrorInfo | None
    artifacts: tuple[ArtifactRef, ...]
    worker: WorkerInfo

    def __post_init__(self):
        normalize_fields(self)
        require(self.schema == RESULT_SCHEMA, "unsupported result schema")
        _match(self.task_id, HEX32, "task_id")
        _match(self.attempt_id, ATTEMPT_ID, "attempt_id")
        require(self.attempt_id.startswith(self.task_id + "-"), "attempt does not belong to task")
        _match(self.lease_token, HEX32, "lease_token")
        require(self.status in RESULT_STATUSES, f"invalid status {self.status!r}")
        require(len(self.artifacts) <= MAX_ARTIFACTS, "too many artifacts")
        names = [ref.name for ref in self.artifacts]
        require(len(names) == len(set(names)), "duplicate artifact names")
        if self.status == "failed":
            require(self.error is not None and self.output is None, "failed result needs error")
            require(not self.artifacts, "failed result must not declare artifacts")
        else:
            require(self.error is None and self.output is not None, "result needs output only")
            require(len(json_text(self.output)) <= MAX_OUTPUT_BYTES, "output too large")

    def record(self) -> dict:
        """The hashed, stored portion: independent of attempt, token and worker runtime."""
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "status": self.status,
            "output": self.output,
            "artifacts": [to_plain(ref) for ref in self.artifacts],
        }

    def result_hash(self) -> str:
        return content_hash(self.record())

    def to_bytes(self) -> bytes:
        return json_text(to_plain(self)).encode()


def parse_result_manifest(raw: bytes) -> ResultManifest:
    """Strictly parse `result.json`; any deviation raises ContractError (→ quarantine)."""
    data = strict_json(raw, MAX_RESULT_MANIFEST_BYTES, "result manifest")
    return from_mapping(ResultManifest, data, "result manifest")
