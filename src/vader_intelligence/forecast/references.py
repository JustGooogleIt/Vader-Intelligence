"""Typed archive reference fingerprints; not availability or identity witnesses."""

import hashlib
import json

from ..provenance import json_text
from .schema import COLUMNS

LEGACY = {
    "fetches",
    "entities",
    "observations",
    "eligibility",
    "settlement_versions",
    "settlement_observations",
    "settlement_targets",
}


def digest(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def run_source_mode(db, run_id):
    """Durable source lineage of a run: "synthetic" or "live" (F3.3 plan §9.3, M1/M2).

    A fixture run is synthetic. A run whose provenance records
    ``tool_configuration.source_lineage`` carries that value. A two-phase collection
    run written before lineage was recorded is synthetic when its own immutable
    discovery summary says so; the inference only ever narrows (never promotes).
    """
    row = db.execute("SELECT kind, provenance_json FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("missing run")
    if row[0] == "fixture":
        return "synthetic"
    tool = json.loads(row[1]).get("tool_configuration") or {}
    lineage = tool.get("source_lineage")
    if lineage in ("synthetic", "live"):
        return lineage
    if lineage is not None:
        raise ValueError(f"unknown source lineage {lineage!r}")
    if (
        tool.get("collection_protocol") is not None
        and db.execute("PRAGMA user_version").fetchone()[0] >= 3
        and db.execute(
            "SELECT 1 FROM discovery_passes WHERE run_id=?"
            " AND json_extract(payload_json,'$.mode')='synthetic' LIMIT 1",
            (run_id,),
        ).fetchone()
    ):
        return "synthetic"
    return "live"


DERIVATIONS = ("current", "draft")


def _synthetic_run(db, run_id, derivation):
    """Whether evidence written by this run is synthetic under a reference derivation.

    "current": the persisted/inferred lineage of `run_source_mode` (PR #12 onward).
    "draft": the frozen derivation of the draft code at PR #10/#11 (5811991), under
    which only fixture runs were synthetic. Used only to validate rows that the draft
    code wrote (decision D-001); never to admit evidence for current use.
    """
    if derivation == "draft":
        row = db.execute("SELECT kind FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("missing run")
        return row[0] == "fixture"
    if derivation != "current":
        raise ValueError("unknown reference derivation")
    return run_source_mode(db, run_id) == "synthetic"


def row_dict(db, table, key):
    if table not in LEGACY | set(COLUMNS):
        raise ValueError("unsupported reference table")
    keys = (
        ("kind", "entity_key", "fetch_id", "parser_version")
        if table == "settlement_observations"
        else (("evaluation_id", "opportunity_key") if table == "evaluation_items" else ("id",))
    )
    if not isinstance(key, dict) or set(key) != set(keys):
        raise ValueError("invalid reference key")
    cursor = db.execute(
        f"SELECT * FROM {table} WHERE " + " AND ".join(k + "=?" for k in keys),
        tuple(key[k] for k in keys),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError("missing reference: " + table)
    return dict(zip((d[0] for d in cursor.description), row))


def reference(db, table, key, derivation="current"):
    """Explicitly selected row → exact typed fingerprint, including retrieval identity.

    Does not choose the latest row, mint receipts, infer entry identity or claim
    visibility. An entry digest/availability receipt belongs in an explicit manifest
    alongside this reference, and is checked by later evidence selection.
    """
    row = row_dict(db, table, key)
    fetch_ids = set()
    mode = "archived"
    kind = row.get("kind")
    if table in COLUMNS:
        mode = json.loads(row["payload_json"])["mode"]
    elif table == "fetches":
        fetch_ids.add(row["id"])
        kind = db.execute("SELECT stage FROM requests WHERE id=?", (row["request_id"],)).fetchone()[
            0
        ]
    else:
        fetch_ids.update(v for k, v in row.items() if k.endswith("fetch_id") and v is not None)
        if "run_id" in row and _synthetic_run(db, row["run_id"], derivation):
            mode = "synthetic"
    fetches = []
    for fetch_id in sorted(fetch_ids):
        cursor = db.execute(
            "SELECT f.seq,f.state,f.body_sha256,f.retrieved_at,b.body,b.byte_count,r.id "
            "FROM fetches f JOIN requests q ON q.id=f.request_id JOIN runs r ON r.id=q.run_id "
            "LEFT JOIN blobs b ON b.sha256=f.body_sha256 WHERE f.id=?",
            (fetch_id,),
        )
        found = cursor.fetchone()
        if not found or found[1] == "pending" or not found[3]:
            raise ValueError("reference is not completed retrieval evidence")
        seq, state, sha, at, body, length, run_id = found
        if sha is not None and (
            body is None or len(body) != length or hashlib.sha256(body).hexdigest() != sha
        ):
            raise ValueError("corrupt reference raw bytes")
        # Lineage is a property of the writing run, persisted in its provenance: a
        # collection made with an injected transport or clock stays synthetic forever.
        if _synthetic_run(db, run_id, derivation):
            mode = "synthetic"
        fetches.append(
            {"id": fetch_id, "seq": seq, "state": state, "raw_sha256": sha, "retrieved_at": at}
        )
    return {
        "table": table,
        "key": key,
        "digest": digest(row),
        "kind": kind,
        "parser_version": row.get("parser_version"),
        "fetches": fetches,
        "source_mode": mode,
    }


def validate_references(db, payload, derivation="current"):
    """Validate every typed nested reference, not just the top-level list."""
    stack = [payload]
    visited = 0
    while stack:
        value = stack.pop()
        visited += 1
        if visited > 100000:
            raise ValueError("manifest node limit")
        if isinstance(value, dict):
            if "table" in value:
                expected = reference(db, value["table"], value.get("key"), derivation)
                if value != expected:
                    raise ValueError("reference provenance mismatch")
                if expected["source_mode"] == "synthetic" and payload["mode"] != "synthetic":
                    raise ValueError("synthetic evidence cannot become live provenance")
            else:
                stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
