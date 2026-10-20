"""Dataset snapshots for research pods (`vi-pod-dataset-v2`).

A snapshot is the only dataset a pod ever reads: the development and validation rows of
one registry dataset under one chronological split, with the feature-set description it
needs to fit models. Embargo and holdout rows are dropped when the snapshot is built, and
`load` re-derives every row's segment from the split, so a snapshot that carries a row
outside its declared segment (a holdout row included by mistake, or a tampered file) is
refused. Since v2 a snapshot also carries the dataset's pinned holdout start (registry
`holdout_pins`): its split must end before it and no row may fall on or after it, so an
operator-chosen split cannot move final-holdout rows into a pod's view. The coordinator
stores snapshots in the orchestration artifact store; workers receive verified copies.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from ..datasets import splits
from ..datasets.splits import SplitDefinition
from ..provenance import json_text

SNAPSHOT_SCHEMA = "vi-pod-dataset-v2"
SNAPSHOT_KIND = "dataset_snapshot"
EXECUTION_KIND = "execution_inputs"
MANIFEST_KEYS = ("name", "evidence_kind", "feature_set")


class SnapshotRefused(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    dataset_id: str
    manifest: dict
    split: SplitDefinition
    development: list
    validation: list
    holdout_from: str

    @property
    def feature_set(self) -> str:
        return self.manifest["feature_set"]["name"]


def feature_set_version(manifest: dict) -> str:
    """Digest of the feature-set description (names, versions, missing policies)."""
    return hashlib.sha256(json_text(manifest["feature_set"]).encode()).hexdigest()[:16]


def build(
    manifest: dict, rows: list, dataset_id: str, split: SplitDefinition, holdout_from: str
) -> bytes:
    """Snapshot bytes; `holdout_from` is the dataset's pinned holdout start
    (`datasets.splits.guard`)."""
    if split.validation_through >= holdout_from:
        raise SnapshotRefused(f"split reaches the pinned holdout (from {holdout_from})")
    development, validation = splits.development_and_validation(rows, split)
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "dataset_id": dataset_id,
        "manifest": {key: manifest[key] for key in MANIFEST_KEYS},
        "split": split.to_dict(),
        "holdout_from": holdout_from,
        "segments": {"development": development, "validation": validation},
    }
    return json_text(payload).encode()


def load(data: bytes) -> Snapshot:
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise SnapshotRefused("snapshot is not JSON") from None
    if isinstance(payload, dict) and payload.get("schema") == "vi-pod-dataset-v1":
        raise SnapshotRefused("vi-pod-dataset-v1 carries no pinned holdout; rebuild the snapshot")
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "dataset_id",
        "manifest",
        "split",
        "holdout_from",
        "segments",
    }:
        raise SnapshotRefused("snapshot keys differ from vi-pod-dataset-v2")
    if payload["schema"] != SNAPSHOT_SCHEMA:
        raise SnapshotRefused("unsupported snapshot schema")
    if set(payload["segments"]) != {"development", "validation"}:
        raise SnapshotRefused("a snapshot holds only development and validation rows")
    if set(payload["manifest"]) != set(MANIFEST_KEYS):
        raise SnapshotRefused("snapshot manifest keys differ")
    raw_split = dict(payload["split"])
    key = raw_split.pop("key", None)
    split = SplitDefinition(**raw_split)
    if key != split.key:
        raise SnapshotRefused("split key does not match its definition")
    holdout_from = payload["holdout_from"]
    try:
        date.fromisoformat(holdout_from)
    except (TypeError, ValueError):
        raise SnapshotRefused("snapshot holdout_from is not a date") from None
    if split.validation_through >= holdout_from:
        raise SnapshotRefused("snapshot split reaches its pinned holdout")
    for segment, rows in payload["segments"].items():
        for row in rows:
            if split.segment(row["official_date"]) != segment:
                raise SnapshotRefused(f"row {row.get('group')} is not in segment {segment}")
            if row["official_date"] >= holdout_from:
                raise SnapshotRefused(f"row {row.get('group')} is in the pinned holdout")
    return Snapshot(
        payload["dataset_id"],
        payload["manifest"],
        split,
        payload["segments"]["development"],
        payload["segments"]["validation"],
        holdout_from,
    )
