"""The versioned research protocol (specification §2), parsed from a separate TOML.

One immutable protocol per cohort, not tunable during it. The collector `[collector]`
parser is untouched; research settings live under `[research]` in their own file. The
canonical configuration hash is the digest of the frozen protocol, so a cohort's
provenance can be recomputed and configuration drift within a cohort is detectable.
"""

import math
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .references import digest

PROTOCOL_ID = "mlb-t60-v1"
BASELINES = ("constant-v1", "midpoint-v1")


@dataclass(frozen=True)
class Protocol:
    """v1 defaults are the specification §2 table. Only the dataset namespace and the
    observer namespace are operational knobs; every numeric is fixed for the cohort."""

    protocol_id: str = PROTOCOL_ID
    dataset: str = "forward-shadow-v1"
    observer_namespace: str = "local-witness-v1"
    horizon_seconds: int = 3600
    book_max_age_seconds: int = 300
    schedule_max_age_seconds: int = 300
    market_max_age_seconds: int = 300
    record_grace_seconds: int = 150
    log_epsilon: str = "0.000001"
    max_games: int = 100
    max_contracts: int = 200
    selection_version: str = "selection-v1"
    policy_version: str = "t60-v1"
    publication_policy: str = "publication-v1"

    def __post_init__(self):
        if self.protocol_id != PROTOCOL_ID:
            raise ValueError("v1 supports only protocol mlb-t60-v1")
        if self.horizon_seconds != 3600:
            raise ValueError("v1 horizon must be 3600")
        for name in (
            "book_max_age_seconds",
            "schedule_max_age_seconds",
            "market_max_age_seconds",
        ):
            if getattr(self, name) != 300:
                raise ValueError(f"{name} is fixed at 300 for v1")
        if self.record_grace_seconds != 150:
            raise ValueError("record_grace_seconds is fixed at 150 for v1")
        if self.log_epsilon != "0.000001":
            raise ValueError("log_epsilon is the exact string 0.000001 for v1")
        for name in ("max_games", "max_contracts"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 10000:
                raise ValueError(f"{name} must be a bounded positive integer")
        for name in (
            "protocol_id",
            "dataset",
            "observer_namespace",
            "selection_version",
            "policy_version",
            "publication_policy",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise ValueError(f"{name} must be a bounded nonempty string")
        # Nonfinite or negative numeric settings are rejected; all are explicit ints
        # or the fixed epsilon string, so there is no float to admit.
        for name, value in asdict(self).items():
            if isinstance(value, float):
                raise ValueError(f"{name} must not be a float")
            if isinstance(value, int) and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")

    @property
    def config_hash(self):
        """Canonical digest of the frozen protocol; the research configuration hash,
        distinct from any collector configuration hash (specification §5)."""
        return digest(asdict(self))

    @classmethod
    def load(cls, path=None, *, dataset=None):
        values = {}
        if path is not None:
            p = Path(path)
            with p.open("rb") as handle:
                parsed = tomllib.load(handle)
            if set(parsed) - {"research"}:
                raise ValueError("only [research] configuration is supported")
            values = parsed.get("research", {})
            allowed = set(asdict(cls()))
            if set(values) - allowed:
                raise ValueError("unknown research configuration field")
        if dataset is not None:
            values["dataset"] = dataset
        return cls(**values)
