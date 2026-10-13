"""Witness committed evidence and append contract-2 receipts at the real observation time.

One finite invocation (F3.3 plan §4.2–§4.4). The caller holds storage.writer_lock. A
separate read-only connection reads committed discovery passes and completed fetches,
their typed references are computed (re-hashing raw bodies), the read is closed, and
only then is the clock sampled: that sample is observed_at for every subject in the
batch, an upper bound on when the evidence became available. Receipts are appended one
per transaction through the journal. No caller-supplied wall time is accepted; an
injected clock forces synthetic lineage. The collector never imports this module.
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime

from ..provenance import RunProvenanceV1, source_hash
from ..storage import timestamp, utc_now
from . import journal
from .references import digest, reference

NAMESPACE = "local-witness-v1"
DIVERGENCE_SECONDS = 2.0
HIGH_WATER_WINDOW = 10000
COMPLETED = ("ok", "parse_error", "error")
# Research subject kinds the tick witnesses alongside passes and fetches. Their receipt
# subject digest is the row's content digest and the reference is the row fingerprint.
RESEARCH_SUBJECTS = {"binding": "forecast_bindings", "publication": "forecast_publications"}
WITNESS_COUNTER = {
    "discovery_pass": "passes_witnessed",
    "fetch": "fetches_witnessed",
    "binding": "bindings_witnessed",
    "publication": "publications_witnessed",
}
REFUSED_UNDER = ("collect", "discover", "live-check")
LIMITS = {"max_subjects": (1, 500), "scan_rows": (1, 5000), "max_passes": (1, 100)}


def witness(
    store,
    *,
    clock=None,
    monotonic=None,
    max_subjects=500,
    scan_rows=5000,
    max_passes=100,
    budget=15.0,
    namespace=NAMESPACE,
):
    """Witness unwitnessed passes and completed fetches; return the run summary dict."""
    injected = clock is not None or monotonic is not None
    clock = clock or utc_now
    monotonic = monotonic or time.monotonic
    for name, (low, high) in LIMITS.items():
        value = locals()[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    if not isinstance(budget, (int, float)) or not 0 < budget <= 300:
        raise ValueError("budget must be in (0, 300] seconds")
    if not isinstance(namespace, str) or not 1 <= len(namespace) <= 64:
        raise ValueError("invalid observer namespace")
    if store.db.in_transaction:
        raise ValueError("witness requires an idle connection")
    if store.schema_version() < 4:
        raise ValueError(
            "witness requires record contract 2: run vader forecast migrate --record-contract 2"
        )
    configuration = {
        "namespace": namespace,
        "max_subjects": max_subjects,
        "scan_rows": scan_rows,
        "max_passes": max_passes,
        "budget": budget,
    }
    config_hash = digest(configuration)
    code_hash = source_hash()
    run_id, session = str(uuid.uuid4()), str(uuid.uuid4())
    store.start_run(
        RunProvenanceV1(run_id, "forecast-witness", code_hash, configuration),
        timestamp(clock()),
    )
    base_utc, base_mono = clock(), monotonic()
    deadline = base_mono + budget
    summary = {
        "run_id": run_id,
        "session_id": session,
        "namespace": namespace,
        "injected_clock": injected,
        "receipts": 0,
        "passes_witnessed": 0,
        "fetches_witnessed": 0,
        "bindings_witnessed": 0,
        "publications_witnessed": 0,
        "untrusted": 0,
        "skipped_existing": 0,
        "skipped_live_passes": 0,
        "pending_seen": 0,
        "interrupted_seen": 0,
        "outside_window": 0,
        "remaining": 0,
        "batches": 0,
        "max_divergence_seconds": 0.0,
        "errors": [],
    }
    status = "complete"
    try:
        while True:
            if monotonic() >= deadline:
                status = "partial"
                break
            batch = _read_batch(
                store.path, namespace, injected, max_passes, scan_rows, max_subjects
            )
            summary.update(
                pending_seen=batch["pending"],
                interrupted_seen=batch["interrupted"],
                outside_window=batch["outside_window"],
                skipped_live_passes=batch["skipped_live_passes"],
                remaining=batch["remaining"],
            )
            if not batch["subjects"]:
                break
            summary["batches"] += 1
            # Observation happens strictly after the read has finished.
            observed = clock()
            offset = max(0.0, monotonic() - base_mono)
            divergence = abs((observed - base_utc).total_seconds() - offset)
            summary["max_divergence_seconds"] = max(summary["max_divergence_seconds"], divergence)
            marks = {
                mode: _high_water_mark(store.db, namespace, mode)
                for mode in {s["mode"] for s in batch["subjects"]}
            }
            for subject in batch["subjects"]:
                if monotonic() >= deadline:
                    status = "partial"
                    break
                trusted = divergence <= DIVERGENCE_SECONDS
                mark, any_receipts = marks[subject["mode"]]
                if any_receipts and (mark is None or observed < mark):
                    trusted = False
                if subject["earliest"] is not None and observed < subject["earliest"]:
                    trusted = False
                created = clock()
                envelope = {
                    "schema_version": 1,
                    "contract_version": 2,
                    "run_id": run_id,
                    "created_at": timestamp(max(created, observed)),
                    "code_hash": code_hash,
                    "config_hash": config_hash,
                    "mode": subject["mode"],
                    "references": [subject["reference"]],
                    "subject_kind": subject["kind"],
                    "fetch_id": subject.get("fetch_id"),
                    "binding_id": subject.get("binding_id"),
                    "publication_id": subject.get("publication_id"),
                    "discovery_id": subject.get("discovery_id"),
                    "subject_digest": subject["digest"],
                    "observed_at": timestamp(observed),
                    "session_id": session,
                    "observer_namespace": namespace,
                    "monotonic_offset": f"{offset:.6f}",
                    "clock_status": "trusted" if trusted else "untrusted",
                }
                before = store.db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM forecast_receipts"
                ).fetchone()[0]
                receipt_id, _ = store.append_forecast("forecast_receipts", envelope)
                if receipt_id <= before:
                    summary["skipped_existing"] += 1
                    continue
                summary["receipts"] += 1
                summary[WITNESS_COUNTER[subject["kind"]]] += 1
                if not trusted:
                    summary["untrusted"] += 1
            else:
                if batch["remaining"] > 0 and summary["receipts"] >= max_subjects:
                    status = "partial"
                    break
                continue
            break
    except journal.IdempotencyConflict as exc:
        # Two different observations for one subject: an integrity failure, never a retry.
        status = "failed"
        summary["errors"].append("integrity: " + str(exc)[:300])
    except (ValueError, sqlite3.Error, OSError) as exc:
        status = "failed"
        summary["errors"].append(str(exc)[:300])
    summary["last_untrusted"] = _last_untrusted(store.db, namespace)
    summary["status"] = status
    summary["elapsed_seconds"] = monotonic() - base_mono
    store.finish_run(run_id, status, summary)
    return summary


def _read_batch(path, namespace, injected, max_passes, scan_rows, max_subjects):
    """Select subjects on a separate read-only snapshot; close it before any clock sample."""
    ro = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
    ro.row_factory = sqlite3.Row
    try:
        ro.execute("BEGIN")
        subjects = []
        skipped_live = 0
        passes = ro.execute(
            "SELECT p.id, p.digest, p.payload_json FROM discovery_passes p WHERE NOT EXISTS ("
            " SELECT 1 FROM forecast_receipts r WHERE r.discovery_id=p.id"
            " AND json_extract(r.payload_json,'$.contract_version')=2"
            " AND json_extract(r.payload_json,'$.observer_namespace')=?"
            " AND json_extract(r.payload_json,'$.mode')=CASE WHEN ? OR"
            " json_extract(p.payload_json,'$.mode')='synthetic' THEN 'synthetic'"
            " ELSE 'forward_shadow' END) ORDER BY p.id DESC LIMIT ?",
            (namespace, int(injected), max_passes),
        ).fetchall()
        for row in passes:
            pass_mode = ro.execute(
                "SELECT json_extract(?, '$.mode'), json_extract(?, '$.created_at')",
                (row["payload_json"], row["payload_json"]),
            ).fetchone()
            if injected and pass_mode[0] != "synthetic":
                skipped_live += 1  # M6: an injected clock can only witness synthetic passes
                continue
            subjects.append(
                {
                    "kind": "discovery_pass",
                    "discovery_id": row["id"],
                    "fetch_id": None,
                    "digest": row["digest"],
                    "reference": reference(ro, "discovery_passes", {"id": row["id"]}),
                    "mode": "synthetic" if pass_mode[0] == "synthetic" else "forward_shadow",
                    "earliest": datetime.fromisoformat(pass_mode[1]),
                    "order": row["id"],
                }
            )
        for kind, table in RESEARCH_SUBJECTS.items():
            id_field = "binding_id" if kind == "binding" else "publication_id"
            rows = ro.execute(
                f"SELECT t.id, t.digest, t.payload_json FROM {table} t WHERE NOT EXISTS ("
                " SELECT 1 FROM forecast_receipts r WHERE r." + id_field + "=t.id"
                " AND json_extract(r.payload_json,'$.contract_version')=2"
                " AND json_extract(r.payload_json,'$.observer_namespace')=?"
                " AND json_extract(r.payload_json,'$.mode')=json_extract(t.payload_json,'$.mode'))"
                " ORDER BY t.id DESC LIMIT ?",
                (namespace, max_passes),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                if injected and payload["mode"] != "synthetic":
                    skipped_live += 1
                    continue
                subjects.append(
                    {
                        "kind": kind,
                        id_field: row["id"],
                        "digest": row["digest"],
                        "reference": reference(ro, table, {"id": row["id"]}),
                        "mode": payload["mode"],
                        "earliest": datetime.fromisoformat(payload["created_at"]),
                        "order": row["id"],
                    }
                )
        window = ro.execute(
            "SELECT f.id, f.seq, f.state, f.retrieved_at, r.kind AS run_kind FROM fetches f"
            " JOIN requests q ON q.id=f.request_id JOIN runs r ON r.id=q.run_id"
            " ORDER BY f.seq DESC LIMIT ?",
            (scan_rows,),
        ).fetchall()
        pending = sum(1 for row in window if row["state"] == "pending")
        interrupted = sum(1 for row in window if row["state"] == "interrupted")
        candidates = []
        for row in window:
            if row["state"] not in COMPLETED or not row["retrieved_at"]:
                continue
            mode = "synthetic" if injected or row["run_kind"] == "fixture" else "forward_shadow"
            exists = ro.execute(
                "SELECT 1 FROM forecast_receipts WHERE fetch_id=?"
                " AND json_extract(payload_json,'$.contract_version')=2"
                " AND json_extract(payload_json,'$.observer_namespace')=?"
                " AND json_extract(payload_json,'$.mode')=? LIMIT 1",
                (row["id"], namespace, mode),
            ).fetchone()
            if exists:
                continue
            candidates.append((row, mode))
        remaining = max(0, len(candidates) - max_subjects)
        chosen = candidates[:max_subjects]  # newest first
        for row, mode in sorted(chosen, key=lambda item: item[0]["seq"]):  # insert ascending
            subjects.append(
                {
                    "kind": "fetch",
                    "discovery_id": None,
                    "fetch_id": row["id"],
                    "reference": None,
                    "digest": None,
                    "mode": mode,
                    "earliest": datetime.fromisoformat(row["retrieved_at"]),
                    "order": row["seq"],
                }
            )
            subjects[-1]["reference"] = reference(ro, "fetches", {"id": row["id"]})
            subjects[-1]["digest"] = subjects[-1]["reference"]["digest"]
        floor = window[-1]["seq"] if len(window) == scan_rows else 0
        outside = ro.execute(
            "SELECT COUNT(*) FROM fetches f WHERE f.seq<? AND f.state IN ('ok','parse_error','error')"
            " AND f.retrieved_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM forecast_receipts r"
            " WHERE r.fetch_id=f.id AND json_extract(r.payload_json,'$.contract_version')=2"
            " AND json_extract(r.payload_json,'$.observer_namespace')=?)",
            (floor, namespace),
        ).fetchone()[0]
        ro.execute("COMMIT")
        return {
            "subjects": subjects,
            "pending": pending,
            "interrupted": interrupted,
            "outside_window": outside,
            "skipped_live_passes": skipped_live,
            "remaining": remaining,
        }
    finally:
        ro.close()


def _high_water_mark(db, namespace, mode):
    """Newest trusted contract-2 observed_at in this namespace and mode, and whether any
    receipt exists there at all (receipts but no trusted one means untrusted)."""
    rows = db.execute(
        "SELECT json_extract(payload_json,'$.clock_status'), observed_at FROM forecast_receipts"
        " WHERE json_extract(payload_json,'$.contract_version')=2"
        " AND json_extract(payload_json,'$.observer_namespace')=?"
        " AND json_extract(payload_json,'$.mode')=? ORDER BY id DESC LIMIT ?",
        (namespace, mode, HIGH_WATER_WINDOW),
    ).fetchall()
    trusted = [datetime.fromisoformat(observed) for status, observed in rows if status == "trusted"]
    return (max(trusted) if trusted else None, bool(rows))


def _last_untrusted(db, namespace):
    row = db.execute(
        "SELECT id, observed_at FROM forecast_receipts"
        " WHERE json_extract(payload_json,'$.contract_version')=2"
        " AND json_extract(payload_json,'$.observer_namespace')=?"
        " AND json_extract(payload_json,'$.clock_status')='untrusted' ORDER BY id DESC LIMIT 1",
        (namespace,),
    ).fetchone()
    return None if row is None else {"id": row[0], "observed_at": row[1]}
