"""Registry of approved, declarative pod operations.

A pod task names an operation registered here and supplies parameters that must satisfy
the operation's typed parameter dataclass; nothing else can run in a worker and no
agent-generated code is ever executed. Workers import only this module to resolve
operations, so an integrator wires a real implementation by registering it here (or in a
module imported from here) with `@register_operation(..., replace=True)`.

An operation may declare input artifacts (`inputs=`): the coordinator checks at submission
that they are stored with the declared kind and copies verified, read-only copies into
the attempt's `in/` directory before spawning; the operation reads them only through
`OperationContext.read_input`, which re-verifies the digest. Inputs are the only data a
worker receives: no registry, archive or holdout path is ever passed.

The research operations' parameter contracts are defined here and their implementations
are registered by `pods.research_ops` (imported at the end of this module). Test
operations (`noop`, `sleep`, `fail`, `crash`, `emit_artifact`) are fully implemented and
permitted only for the `diagnostics` role.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import (
    ALLOWED_SPLITS,
    ARTIFACT_KIND,
    ARTIFACT_NAME,
    HEX32,
    HEX64,
    OPERATION_NAME,
    ROLES,
    ArtifactRef,
    ContractError,
    from_mapping,
    normalize_fields,
    require,
    to_plain,
)


class OperationNotWired(RuntimeError):
    """A research operation's contract exists but its implementation is not wired yet."""


class UnknownOperation(LookupError):
    pass


class DeterministicFailure(RuntimeError):
    """The operation failed in a way a retry cannot change; never retried."""


class TransientFailure(RuntimeError):
    """The operation failed in a way a retry may fix; retried with backoff."""


NOT_RETRYABLE = (ContractError, UnknownOperation, OperationNotWired, DeterministicFailure)
MAX_INPUT_BYTES = 256 * 1024 * 1024
INPUT_DIR = "in"
INPUT_MANIFEST = "inputs.json"


@dataclass(frozen=True)
class Partial:
    """Return value for an operation that stopped early with a usable partial output."""

    output: dict


class OperationContext:
    """What an operation may touch: its attempt identity and its private output dir."""

    def __init__(self, task_id, attempt_id, attempt_n, workdir, deadline_at):
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.attempt_n = attempt_n
        self.workdir = Path(workdir)
        self.out_dir = self.workdir / "out"
        self.in_dir = self.workdir / INPUT_DIR
        self.deadline_at = deadline_at
        self.artifacts: list[ArtifactRef] = []

    def emit(self, name: str, data: bytes, kind: str) -> ArtifactRef:
        """Write one output file (create-only, no links) and declare it in the manifest."""
        require(isinstance(name, str) and bool(ARTIFACT_NAME.match(name)), "unsafe artifact name")
        require(isinstance(kind, str) and bool(ARTIFACT_KIND.match(kind)), "invalid artifact kind")
        require(isinstance(data, bytes), "artifact data must be bytes")
        self.out_dir.mkdir(exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(self.out_dir / name, flags, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        ref = ArtifactRef(name, kind, hashlib.sha256(data).hexdigest(), len(data))
        self.artifacts.append(ref)
        return ref

    def read_input(self, sha256: str) -> bytes:
        """Bytes of a staged input artifact (no links), re-verified against its digest."""
        require(isinstance(sha256, str) and bool(HEX64.match(sha256)), "input id must be sha256")
        try:
            fd = os.open(self.in_dir / sha256, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise DeterministicFailure(
                f"input {sha256} not staged ({type(exc).__name__})"
            ) from None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT_BYTES:
                raise DeterministicFailure(f"input {sha256} is not a bounded regular file")
            data = handle.read(MAX_INPUT_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise DeterministicFailure(f"input {sha256} fails verification")
        return data

    def staged_inputs(self) -> dict[str, list[str]]:
        """Artifact kind → staged input digests, as declared by the coordinator."""
        try:
            declared = json.loads((self.in_dir / INPUT_MANIFEST).read_bytes())
        except (OSError, ValueError):
            return {}
        out: dict[str, list[str]] = {}
        for item in declared:
            out.setdefault(item["kind"], []).append(item["sha256"])
        return out


def no_inputs(params, store) -> tuple:
    return ()


@dataclass(frozen=True)
class OperationSpec:
    name: str
    params: type
    roles: tuple[str, ...]
    run: Callable[[Any, OperationContext], Any]
    # (params, store) -> ((sha256, artifact kind), ...) the coordinator stages for the worker
    inputs: Callable[[Any, Any], tuple] = no_inputs

    def parse(self, params: Any):
        """Typed parameter instance; raises ContractError for anything else."""
        return from_mapping(self.params, params, f"{self.name} params")


REGISTRY: dict[str, OperationSpec] = {}


def register_operation(name: str, params: type, *, roles, replace: bool = False, inputs=None):
    """Decorator registering an approved operation with its parameter contract, roles and
    (optionally) the input artifacts it reads."""
    if not OPERATION_NAME.match(name):
        raise ValueError(f"invalid operation name {name!r}")
    roles = tuple(roles)
    if not roles or not set(roles) <= set(ROLES):
        raise ValueError(f"operation roles must be a non-empty subset of {ROLES}")
    if name in REGISTRY and not replace:
        raise ValueError(f"operation {name!r} is already registered")

    def decorate(function):
        REGISTRY[name] = OperationSpec(name, params, roles, function, inputs or no_inputs)
        return function

    return decorate


def get_operation(name: str) -> OperationSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise UnknownOperation(f"operation {name!r} is not approved") from None


def normalized_params(name: str, params: Any) -> dict:
    """Validated, canonical parameters (the form that is hashed into the task id)."""
    return to_plain(get_operation(name).parse(params))


def _hex64(value: str, what: str) -> None:
    require(bool(HEX64.match(value)), f"{what} must be a sha256 hex digest")


def _development_split(split: str) -> None:
    require(split in ALLOWED_SPLITS, f"split must be one of {list(ALLOWED_SPLITS)}")


# Test operations (diagnostics role only) ------------------------------------------------

DIAGNOSTICS = ("diagnostics",)
MAX_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class NoopParams:
    def __post_init__(self):
        normalize_fields(self)


@register_operation("noop", NoopParams, roles=DIAGNOSTICS)
def noop(params: NoopParams, ctx: OperationContext):
    return {"ok": True}


@dataclass(frozen=True)
class SleepParams:
    seconds: float
    with_child: bool = False

    def __post_init__(self):
        normalize_fields(self)
        require(
            0 <= self.seconds <= MAX_SLEEP_SECONDS, f"seconds must be in 0..{MAX_SLEEP_SECONDS}"
        )


@register_operation("sleep", SleepParams, roles=DIAGNOSTICS)
def sleep(params: SleepParams, ctx: OperationContext):
    """Bounded sleep; `with_child` also sleeps in a same-group child (group-kill evidence)."""
    child = None
    if params.with_child:
        child = subprocess.Popen(
            [sys.executable, "-s", "-P", "-c", f"import time; time.sleep({params.seconds!r})"],
            stdin=subprocess.DEVNULL,
        )
        (ctx.workdir / "child.pid").write_text(str(child.pid))
    time.sleep(params.seconds)
    if child is not None:
        child.wait(timeout=MAX_SLEEP_SECONDS)
    return {"slept": params.seconds, "with_child": params.with_child}


@dataclass(frozen=True)
class FailParams:
    mode: str
    fail_attempts: int = 1000
    message: str = "requested failure"

    def __post_init__(self):
        normalize_fields(self)
        require(self.mode in ("deterministic", "transient"), "mode must be deterministic|transient")
        require(1 <= self.fail_attempts <= 1000, "fail_attempts must be in 1..1000")
        require(len(self.message) <= 200, "message too long")


@register_operation("fail", FailParams, roles=DIAGNOSTICS)
def fail(params: FailParams, ctx: OperationContext):
    """Deterministic: always fails. Transient: fails on attempts <= fail_attempts."""
    if params.mode == "deterministic":
        raise DeterministicFailure(params.message)
    if ctx.attempt_n <= params.fail_attempts:
        raise TransientFailure(params.message)
    return {"recovered_on_attempt": ctx.attempt_n}


@dataclass(frozen=True)
class CrashParams:
    exit_code: int = 70
    crash_attempts: int = 1
    after_seconds: float = 0.0

    def __post_init__(self):
        normalize_fields(self)
        require(1 <= self.exit_code <= 125, "exit_code must be in 1..125")
        require(1 <= self.crash_attempts <= 1000, "crash_attempts must be in 1..1000")
        require(0 <= self.after_seconds <= MAX_SLEEP_SECONDS, "after_seconds out of range")


@register_operation("crash", CrashParams, roles=DIAGNOSTICS)
def crash(params: CrashParams, ctx: OperationContext):
    """Exit abruptly (no manifest, no cleanup) on attempts <= crash_attempts."""
    if ctx.attempt_n <= params.crash_attempts:
        time.sleep(params.after_seconds)
        os._exit(params.exit_code)
    return {"survived_on_attempt": ctx.attempt_n}


@dataclass(frozen=True)
class EmitArtifactParams:
    name: str
    text: str
    kind: str = "test_output"

    def __post_init__(self):
        normalize_fields(self)
        require(bool(ARTIFACT_NAME.match(self.name)), "unsafe artifact name")
        require(bool(ARTIFACT_KIND.match(self.kind)), "invalid artifact kind")
        require(len(self.text.encode()) <= 1 << 20, "text larger than 1 MiB")


@register_operation("emit_artifact", EmitArtifactParams, roles=DIAGNOSTICS)
def emit_artifact(params: EmitArtifactParams, ctx: OperationContext):
    ref = ctx.emit(params.name, params.text.encode(), params.kind)
    return {"name": ref.name, "sha256": ref.sha256, "bytes": ref.bytes}


# Research operation contracts (implementations: `pods.research_ops`) --------------------

TRAIN_HYPERPARAMETERS = ("grid", "calibration", "n_folds", "max_configs")
CALIBRATIONS = ("platt", "isotonic", "none")
DIAGNOSTIC_METRICS = ("log_loss", "brier", "ece")
PERTURBATIONS = (
    "by_month",
    "regular_season_only",
    "postseason_only",
    "tight_spread",
    "wide_spread",
)
FEATURE_DROP_PREFIX = "drop_feature:"
FEE_SCHEDULE_VERSIONS = ("kalshi-2026-02-general", "kalshi-2026-02-mlb-multiplier-0.5-assumed")


def _decimal_text(value: str, what: str, low: float, high: float) -> None:
    try:
        number = float(value)
    except ValueError:
        raise ContractError(f"{what} must be a decimal string") from None
    require(low <= number <= high and value.strip() == value, f"{what} out of range")


@dataclass(frozen=True)
class TrainModelParams:
    """Fit on the snapshot's development rows (the only fit set); validation is reported
    by the pod as a diagnostic and recomputed by the protected evaluator."""

    dataset_manifest_hash: str
    feature_set: str
    feature_set_version: str
    model_family: str
    seed: int
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    split: str = "development"

    def __post_init__(self):
        normalize_fields(self)
        _hex64(self.dataset_manifest_hash, "dataset_manifest_hash")
        require(bool(self.feature_set and self.feature_set_version), "feature set required")
        require(self.model_family in ("logistic", "gradient_boosting"), "unknown model_family")
        require(self.seed >= 0, "seed must be non-negative")
        require(self.split == "development", "models are fit on development rows only")
        unknown = sorted(set(self.hyperparameters) - set(TRAIN_HYPERPARAMETERS))
        require(not unknown, f"unknown hyperparameters {unknown}")
        h = self.hyperparameters
        require(h.get("calibration", "platt") in CALIBRATIONS, "unknown calibration")
        require(2 <= h.get("n_folds", 4) <= 10, "n_folds must be in 2..10")
        require(1 <= h.get("max_configs", 24) <= 256, "max_configs must be in 1..256")
        grid = h.get("grid", {})
        require(isinstance(grid, dict), "grid must be an object of lists")
        require(all(isinstance(v, list) and v for v in grid.values()), "grid values: lists")


@dataclass(frozen=True)
class SymbolicSearchParams:
    dataset_manifest_hash: str
    seed: int
    max_candidates: int
    max_generations: int
    max_seconds: float
    operator_set: str = "ops-v1"
    population: int = 200

    def __post_init__(self):
        normalize_fields(self)
        _hex64(self.dataset_manifest_hash, "dataset_manifest_hash")
        require(self.seed >= 0, "seed must be non-negative")
        require(1 <= self.max_candidates <= 1_000_000, "max_candidates out of range")
        require(1 <= self.max_generations <= 10_000, "max_generations out of range")
        require(0 < self.max_seconds <= 86400, "max_seconds out of range")
        require(self.operator_set == "ops-v1", "operator_set must be ops-v1")
        require(10 <= self.population <= 10_000, "population out of range")


@dataclass(frozen=True)
class EvaluateCandidateParams:
    """Pod-side diagnostics on development/validation only; never promotion evidence."""

    candidate_id: str
    dataset_manifest_hash: str
    split: str
    metrics: tuple[str, ...] = ("log_loss", "brier")

    def __post_init__(self):
        normalize_fields(self)
        require(bool(HEX32.match(self.candidate_id)), "candidate_id must be 32 hex characters")
        _hex64(self.dataset_manifest_hash, "dataset_manifest_hash")
        _development_split(self.split)
        require(bool(self.metrics), "at least one metric required")
        require(set(self.metrics) <= set(DIAGNOSTIC_METRICS), "unknown metric")


@dataclass(frozen=True)
class RobustnessAnalysisParams:
    """Slices of the validation rows on which the candidate is compared with the market."""

    candidate_id: str
    dataset_manifest_hash: str
    perturbations: tuple[str, ...]
    seed: int

    def __post_init__(self):
        normalize_fields(self)
        require(bool(HEX32.match(self.candidate_id)), "candidate_id must be 32 hex characters")
        _hex64(self.dataset_manifest_hash, "dataset_manifest_hash")
        require(bool(self.perturbations), "at least one perturbation required")
        require(len(set(self.perturbations)) == len(self.perturbations), "duplicate perturbation")
        for name in self.perturbations:
            ok = name in PERTURBATIONS or (
                name.startswith(FEATURE_DROP_PREFIX)
                and bool(ARTIFACT_KIND.match(name[len(FEATURE_DROP_PREFIX) :].replace("@", "_")))
            )
            require(ok, f"unknown perturbation {name!r}")
        require(self.seed >= 0, "seed must be non-negative")


@dataclass(frozen=True)
class PaperBacktestParams:
    """Historical paper backtest of a candidate on the validation rows: candle execution
    inputs built by the coordinator, conservative execution, labelled depth scenarios."""

    candidate_id: str
    dataset_manifest_hash: str
    fee_schedule_version: str
    execution_inputs_hash: str
    execution_assumptions: str = "conservative"
    min_edge: str = "0.02"
    assumed_depths: tuple[str, ...] = ("10",)

    def __post_init__(self):
        normalize_fields(self)
        require(bool(HEX32.match(self.candidate_id)), "candidate_id must be 32 hex characters")
        _hex64(self.dataset_manifest_hash, "dataset_manifest_hash")
        _hex64(self.execution_inputs_hash, "execution_inputs_hash")
        require(self.fee_schedule_version in FEE_SCHEDULE_VERSIONS, "unknown fee schedule")
        require(self.execution_assumptions == "conservative", "only conservative execution")
        _decimal_text(self.min_edge, "min_edge", 0.0, 0.5)
        require(len(self.assumed_depths) <= 4, "at most four depth scenarios")
        for depth in self.assumed_depths:
            _decimal_text(depth, "assumed depth", 0.01, 100000.0)


from . import research_ops  # noqa: E402,F401  (registers the research operations)
