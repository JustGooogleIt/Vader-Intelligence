"""Witness committed evidence and append contract-2 receipts at the real observation time.

One finite invocation (F3.3 plan §4.2–§4.4). The caller holds storage.writer_lock. A
separate read-only connection reads committed discovery passes, research subjects and
completed fetches, their typed references are computed (re-hashing raw bodies), the read
is closed, and only then is the clock sampled: that sample is observed_at for every
subject in the batch, an upper bound on when the evidence became available. Receipts are
appended one per transaction through the journal. No caller-supplied wall time is
accepted; an injected clock forces synthetic lineage. The collector never imports this
module.

Operational-witness boundary: a contract-2 receipt is admissible only when written by a
``forecast-witness`` run whose provenance names the receipt's observer namespace and
session (enforced by the journal), so a receipt supplied through the journal API by any
other caller is never availability proof and can never occupy a witness receipt key.
Only this module creates that run kind (``Store.start_run`` refuses it), and a receipt
observed before its witness run started is refused by the journal and is not proof.

Lineage: a fetch's mode comes from its writing run's persisted source lineage
(``references.run_source_mode``), never from the witness clock alone; an injected clock
additionally forces every receipt synthetic. Subjects that cannot legally receive an
operational receipt (historical-reconstruction passes or bindings, live subjects under
an injected clock) are skipped and reported, never converted.

Bounds are per invocation: at most ``max_passes`` discovery passes, ``max_passes``
research subjects (bindings and publications) and ``max_subjects`` fetches, cumulative
across batches. Reaching a cap with eligible subjects left finishes ``partial`` with the
remaining count per kind.
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime

from ..provenance import RunProvenanceV1, source_hash
from ..storage import timestamp, utc_now
from . import journal
from .evidence import WITNESS_WRITTEN
from .references import digest, reference, run_source_mode

NAMESPACE = "local-witness-v1"
RUN_KIND = "forecast-witness"
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
GROUP = {
    "discovery_pass": "discovery_pass",
    "fetch": "fetch",
    "binding": "research",
    "publication": "research",
}
REFUSED_UNDER = ("collect", "discover", "live-check")
LIMITS = {"max_subjects": (1, 500), "scan_rows": (1, 5000), "max_passes": (1, 100)}
# A subject is witnessed once: the first receipt a recorded witness run wrote is permanent
# even when it fails the run-start bound (evidence.WITNESS_WRITTEN), so such a subject is
# left unproven and never re-observed, which would be an IdempotencyConflict that fails
# every later tick. Only operational witness receipts count, whatever a payload claims: a
# subject already witnessed under contract 2 by a lineage-recording witness is not
# witnessed again; a draft-era or supplied receipt does not count as witnessed.
WITNESS_RECEIPT = WITNESS_WRITTEN
CANDIDATE_LIMIT = 10000


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
    """Witness unwitnessed passes, research subjects and completed fetches.

    Returns the run summary dict (status complete | partial | failed).
    """
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
    if store.schema_version() < 5:
        raise ValueError(
            "witness requires record contract 3: run vader forecast migrate --record-contract 3"
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
    lineage = "synthetic" if injected else "live"
    # One clock sample is both the run's start and the base for the divergence check, so
    # a clock that steps back after the run starts counts against trust and can never be
    # absorbed into a later base (a receipt may not predate its run, review F3).
    base_utc, base_mono = clock(), monotonic()
    _start_run(
        store,
        RunProvenanceV1(
            run_id,
            RUN_KIND,
            code_hash,
            configuration,
            tool_configuration={
                "observer_namespace": namespace,
                "session_id": session,
                "source_lineage": lineage,
            },
        ),
        timestamp(base_utc),
    )
    deadline = base_mono + budget
    allowance = {"discovery_pass": max_passes, "research": max_passes, "fetch": max_subjects}
    summary = {
        "run_id": run_id,
        "session_id": session,
        "namespace": namespace,
        "injected_clock": injected,
        "source_lineage": lineage,
        "receipts": 0,
        "passes_witnessed": 0,
        "fetches_witnessed": 0,
        "bindings_witnessed": 0,
        "publications_witnessed": 0,
        "untrusted": 0,
        "skipped_existing": 0,
        "skipped_live_passes": 0,
        "skipped_legacy_subjects": 0,
        "skipped_invalid": 0,
        "pending_seen": 0,
        "interrupted_seen": 0,
        "outside_window": 0,
        "remaining": 0,
        "remaining_by_kind": {"discovery_pass": 0, "research": 0, "fetch": 0},
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
            batch = _read_batch(store.path, namespace, injected, allowance, scan_rows)
            summary.update(
                pending_seen=batch["pending"],
                interrupted_seen=batch["interrupted"],
                outside_window=batch["outside_window"],
                skipped_live_passes=batch["skipped_live"],
                skipped_legacy_subjects=batch["skipped_legacy"],
                remaining_by_kind=batch["remaining"],
                remaining=sum(batch["remaining"].values()),
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
                allowance[GROUP[subject["kind"]]] -= 1
                trusted = divergence <= DIVERGENCE_SECONDS
                mark, any_receipts = marks[subject["mode"]]
                if any_receipts and (mark is None or observed < mark):
                    trusted = False
                if subject["earliest"] is not None and observed < subject["earliest"]:
                    trusted = False
                created = clock()
                envelope = {
                    "schema_version": 1,
                    "contract_version": 3,
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
                try:
                    receipt_id, _ = store.append_forecast("forecast_receipts", envelope)
                except journal.IdempotencyConflict:
                    raise
                except ValueError as exc:
                    # One subject the journal refuses (for example a reference that no
                    # longer verifies) is reported and skipped; it stays unproven and the
                    # rest of the batch continues.
                    summary["skipped_invalid"] += 1
                    if len(summary["errors"]) < 100:
                        summary["errors"].append(
                            f"{subject['kind']} {subject['identifier']}: {str(exc)[:200]}"
                        )
                    continue
                if receipt_id <= before:
                    summary["skipped_existing"] += 1
                    continue
                summary["receipts"] += 1
                summary[WITNESS_COUNTER[subject["kind"]]] += 1
                if not trusted:
                    summary["untrusted"] += 1
            else:
                if any(
                    batch["remaining"][group] > 0 and allowance[group] <= 0 for group in allowance
                ):
                    status = "partial"  # an invocation cap was reached with work left
                    break
                if not any(batch["remaining"].values()):
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
    if summary["errors"] and status == "complete":
        status = "partial"
    summary["last_untrusted"] = _last_untrusted(store.db, namespace)
    summary["status"] = status
    summary["elapsed_seconds"] = monotonic() - base_mono
    store.finish_run(run_id, status, summary)
    return summary


def _start_run(store, provenance, started_at):
    """The only writer of forecast-witness runs (Store.start_run refuses the kind). The
    start is sampled before any observation, and the journal and PROOF refuse a receipt
    observed before its witness run started."""
    with store.transaction():
        store.db.execute(
            "INSERT INTO runs(id,kind,started_at,status,provenance_json) VALUES (?,?,?,?,?)",
            (provenance.run_id, RUN_KIND, started_at, "running", provenance.to_json()),
        )


def _receipt_mode(injected, subject_mode):
    """Mode of the receipt the witness would write for a subject of the given mode."""
    return "synthetic" if injected or subject_mode == "synthetic" else "forward_shadow"


def _read_batch(path, namespace, injected, allowance, scan_rows):
    """Select subjects on a separate read-only snapshot; close it before any clock sample.

    Returns the chosen subjects (within the remaining per-invocation allowance) and the
    count of eligible subjects left per kind.
    """
    ro = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
    ro.row_factory = sqlite3.Row
    try:
        ro.execute("BEGIN")
        subjects = []
        skipped_live = skipped_legacy = 0
        remaining = {"discovery_pass": 0, "research": 0, "fetch": 0}
        # Discovery passes, newest first, lacking a witness receipt in this namespace
        # for the mode the witness would write.
        rows = ro.execute(
            "SELECT p.id, p.digest, json_extract(p.payload_json,'$.mode') AS mode,"
            " json_extract(p.payload_json,'$.created_at') AS created_at FROM discovery_passes p"
            " WHERE NOT EXISTS (SELECT 1 FROM forecast_receipts r WHERE r.discovery_id=p.id AND"
            + WITNESS_RECEIPT
            + " AND json_extract(r.payload_json,'$.mode')=CASE WHEN ? OR"
            " json_extract(p.payload_json,'$.mode')='synthetic' THEN 'synthetic'"
            " ELSE 'forward_shadow' END) ORDER BY p.id DESC LIMIT ?",
            (namespace, int(injected), CANDIDATE_LIMIT),
        ).fetchall()
        eligible = []
        for row in rows:
            if row["mode"] == "historical_reconstruction":
                skipped_legacy += 1  # M6: a reconstruction pass never gets an operational receipt
                continue
            if injected and row["mode"] != "synthetic":
                skipped_live += 1  # M6: an injected clock can only witness synthetic passes
                continue
            eligible.append(row)
        chosen, remaining["discovery_pass"] = _take(eligible, allowance["discovery_pass"])
        for row in chosen:
            subjects.append(
                {
                    "kind": "discovery_pass",
                    "identifier": row["id"],
                    "discovery_id": row["id"],
                    "fetch_id": None,
                    "digest": row["digest"],
                    "reference": reference(ro, "discovery_passes", {"id": row["id"]}),
                    "mode": _receipt_mode(injected, row["mode"]),
                    "earliest": datetime.fromisoformat(row["created_at"]),
                }
            )
        research = []
        for kind, table in RESEARCH_SUBJECTS.items():
            id_field = "binding_id" if kind == "binding" else "publication_id"
            rows = ro.execute(
                f"SELECT t.id, t.digest, t.payload_json FROM {table} t WHERE NOT EXISTS ("
                " SELECT 1 FROM forecast_receipts r WHERE r."
                + id_field
                + "=t.id AND"
                + WITNESS_RECEIPT
                + " AND json_extract(r.payload_json,'$.mode')=json_extract(t.payload_json,'$.mode'))"
                " ORDER BY t.id DESC LIMIT ?",
                (namespace, CANDIDATE_LIMIT),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                if payload["mode"] == "historical_reconstruction":
                    skipped_legacy += 1  # M7: no receipt on a reconstruction binding
                    continue
                if injected and payload["mode"] != "synthetic":
                    skipped_live += 1
                    continue
                research.append((kind, table, id_field, row, payload))
        chosen, remaining["research"] = _take(research, allowance["research"])
        for kind, table, id_field, row, payload in chosen:
            subjects.append(
                {
                    "kind": kind,
                    "identifier": row["id"],
                    id_field: row["id"],
                    "digest": row["digest"],
                    "reference": reference(ro, table, {"id": row["id"]}),
                    "mode": payload["mode"],
                    "earliest": datetime.fromisoformat(payload["created_at"]),
                }
            )
        window = ro.execute(
            "SELECT f.id, f.seq, f.state, f.retrieved_at, q.run_id FROM fetches f"
            " JOIN requests q ON q.id=f.request_id ORDER BY f.seq DESC LIMIT ?",
            (scan_rows,),
        ).fetchall()
        pending = sum(1 for row in window if row["state"] == "pending")
        interrupted = sum(1 for row in window if row["state"] == "interrupted")
        lineage = {}
        candidates = []
        for row in window:
            if row["state"] not in COMPLETED or not row["retrieved_at"]:
                continue
            if row["run_id"] not in lineage:
                lineage[row["run_id"]] = run_source_mode(ro, row["run_id"])
            mode = _receipt_mode(injected, lineage[row["run_id"]])
            exists = ro.execute(
                "SELECT 1 FROM forecast_receipts r WHERE r.fetch_id=? AND"
                + WITNESS_RECEIPT
                + " AND json_extract(r.payload_json,'$.mode')=? LIMIT 1",
                (row["id"], namespace, mode),
            ).fetchone()
            if exists:
                continue
            candidates.append((row, mode))
        chosen, remaining["fetch"] = _take(candidates, allowance["fetch"])  # newest first
        for row, mode in sorted(chosen, key=lambda item: item[0]["seq"]):  # insert ascending
            typed = reference(ro, "fetches", {"id": row["id"]})
            subjects.append(
                {
                    "kind": "fetch",
                    "identifier": row["id"],
                    "discovery_id": None,
                    "fetch_id": row["id"],
                    "reference": typed,
                    "digest": typed["digest"],
                    "mode": mode,
                    "earliest": datetime.fromisoformat(row["retrieved_at"]),
                }
            )
        floor = window[-1]["seq"] if len(window) == scan_rows else 0
        outside = ro.execute(
            "SELECT COUNT(*) FROM fetches f WHERE f.seq<? AND f.state IN ('ok','parse_error','error')"
            " AND f.retrieved_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM forecast_receipts r"
            " WHERE r.fetch_id=f.id AND" + WITNESS_RECEIPT + ")",
            (floor, namespace),
        ).fetchone()[0]
        ro.execute("COMMIT")
        return {
            "subjects": subjects,
            "pending": pending,
            "interrupted": interrupted,
            "outside_window": outside,
            "skipped_live": skipped_live,
            "skipped_legacy": skipped_legacy,
            "remaining": remaining,
        }
    finally:
        ro.close()


def _take(items, allowance):
    """The first `allowance` items and how many eligible items are left over."""
    allowance = max(0, allowance)
    return items[:allowance], max(0, len(items) - allowance)


def _high_water_mark(db, namespace, mode):
    """Newest trusted operational observed_at in this namespace and mode, and whether any
    witness receipt exists there at all (receipts but no trusted one means untrusted)."""
    rows = db.execute(
        "SELECT json_extract(r.payload_json,'$.clock_status'), r.observed_at"
        " FROM forecast_receipts r WHERE"
        + WITNESS_RECEIPT
        + " AND json_extract(r.payload_json,'$.mode')=? ORDER BY r.id DESC LIMIT ?",
        (namespace, mode, HIGH_WATER_WINDOW),
    ).fetchall()
    trusted = [datetime.fromisoformat(observed) for status, observed in rows if status == "trusted"]
    return (max(trusted) if trusted else None, bool(rows))


def _last_untrusted(db, namespace):
    row = db.execute(
        "SELECT r.id, r.observed_at FROM forecast_receipts r WHERE"
        + WITNESS_RECEIPT
        + " AND json_extract(r.payload_json,'$.clock_status')='untrusted' ORDER BY r.id DESC LIMIT 1",
        (namespace,),
    ).fetchone()
    return None if row is None else {"id": row[0], "observed_at": row[1]}
