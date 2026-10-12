"""Read-side availability projection (F3.3 plan §4.6). Read-only, no clock, no writes.

Turns stored discovery passes, fetches and contract-2 receipts into the arguments the
pure forecast.policy functions accept. Only contract-2 receipts with a trusted clock are
proof; a contract-1 receipt contributes nothing whatever time it claims. Sequence
numbers order records that are already witnessed and never establish availability.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from .discovery import COLLECTION_PROTOCOL, MANIFEST_VERSION
from .policy import EvidenceTimes, freshness, utc
from .references import digest

NAMESPACE = "local-witness-v1"
DISCOVERY_STAGES = ("status", "series", "document", "markets", "event")
PASS_RUN_KINDS = ("collect", "discover", "live-check", "fixture")


@dataclass(frozen=True)
class Availability:
    fetch_id: str
    status: str  # witnessed | availability_unproven | clock_untrusted | missing | incomplete
    retrieved_at: datetime | None
    observed_at: datetime | None
    receipt_id: int | None

    @property
    def times(self):
        if self.retrieved_at is None:
            return None
        return EvidenceTimes(self.retrieved_at, self.observed_at)


@dataclass(frozen=True)
class DiscoveryEvidence:
    cutoff: datetime
    kind: str | None  # pass | orphan | None
    pass_id: int | None
    run_id: str | None
    universe_complete: bool
    reasons: tuple
    completed_at: datetime | None
    observed_at: datetime | None
    receipt_id: int | None
    collector_config_hash: str | None
    diagnostics: dict

    @property
    def times(self):
        if self.completed_at is None:
            return None
        return EvidenceTimes(self.completed_at, self.observed_at)

    def eligibility_reasons(self):
        """Discovery part of the common cutoff reasons, in the policy's fixed order."""
        reasons = []
        check = freshness(self.times, self.cutoff)
        reasons.extend("discovery:" + r for r in check.reasons if r != "input_stale")
        if not self.universe_complete or not check.eligible:
            reasons.append("universe_incomplete")
        if "input_stale" in check.reasons:
            reasons.append("discovery:input_stale")
        reasons.extend(r for r in self.reasons if r not in reasons)
        return tuple(reasons)


def _receipt_filter(namespace, mode):
    return (
        " json_extract(r.payload_json,'$.contract_version')=2"
        " AND json_extract(r.payload_json,'$.observer_namespace')=?"
        " AND json_extract(r.payload_json,'$.mode')=?",
        (namespace, mode),
    )


def fetch_availability(db, fetch_id, namespace=NAMESPACE, mode="forward_shadow"):
    row = db.execute("SELECT state, retrieved_at FROM fetches WHERE id=?", (fetch_id,)).fetchone()
    if row is None:
        return Availability(fetch_id, "missing", None, None, None)
    state, retrieved = row[0], row[1]
    if state not in ("ok", "parse_error", "error") or not retrieved:
        return Availability(fetch_id, "incomplete", None, None, None)
    retrieved_at = datetime.fromisoformat(retrieved)
    where, params = _receipt_filter(namespace, mode)
    receipts = db.execute(
        "SELECT r.id, r.observed_at, json_extract(r.payload_json,'$.clock_status')"
        " FROM forecast_receipts r WHERE r.fetch_id=? AND" + where + " ORDER BY r.id",
        (fetch_id, *params),
    ).fetchall()
    if not receipts:
        return Availability(fetch_id, "availability_unproven", retrieved_at, None, None)
    # The first contract-2 receipt for a subject is permanent (plan §4.3).
    receipt_id, observed, clock_status = receipts[0]
    if clock_status != "trusted":
        return Availability(fetch_id, "clock_untrusted", retrieved_at, None, receipt_id)
    return Availability(
        fetch_id, "witnessed", retrieved_at, datetime.fromisoformat(observed), receipt_id
    )


def _run_facts(db, run_id, cache):
    if run_id not in cache:
        row = db.execute("SELECT kind, provenance_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            cache[run_id] = None
        else:
            provenance = json.loads(row[1])
            cache[run_id] = {
                "kind": row[0],
                "config_hash": digest(provenance["configuration"]),
                "protocol": provenance.get("tool_configuration", {}).get("collection_protocol"),
            }
    return cache[run_id]


def verify_pass(payload, run):
    """Reader-side checks a complete pass must satisfy before it is a usable universe."""
    problems = []
    manifest = payload["manifest"]
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        problems.append("manifest_unknown")
    elif not (
        manifest.get("phase_reached") == "done"
        and manifest.get("candidates_known") is True
        and manifest.get("eligibility_complete") is True
        and manifest.get("pages_complete") is True
    ):
        problems.append("pass_inconsistent")
    if payload["reasons"]:
        problems.append("pass_inconsistent")
    if payload["mode"] == "historical_reconstruction":
        problems.append("pass_mode_invalid")
    if run is None or run["kind"] not in PASS_RUN_KINDS:
        problems.append("run_kind_invalid")
    elif run["config_hash"] != payload["config_hash"]:
        problems.append("collector_config_mismatch")
    if run is not None and run["protocol"] != COLLECTION_PROTOCOL:
        problems.append("run_not_two_phase")
    return tuple(dict.fromkeys(problems))


def discovery_as_of(db, cutoff, namespace=NAMESPACE, mode="forward_shadow"):
    """Applicable discovery record for cutoff C, using only records witnessed by C."""
    cutoff = utc(cutoff)
    where, params = _receipt_filter(namespace, mode)
    runs = {}
    diagnostics = {
        "passes_witnessed_by_cutoff": 0,
        "passes_first_witnessed_after_cutoff": 0,
        "untrusted_receipts_ignored": 0,
        "contract_1_receipts_ignored": db.execute(
            "SELECT COUNT(*) FROM forecast_receipts r WHERE r.subject_kind IN"
            " ('discovery_pass','fetch') AND json_extract(r.payload_json,'$.contract_version')"
            " IS NULL"
        ).fetchone()[0],
        "orphan_runs": 0,
    }
    passes = []
    for row in db.execute(
        "SELECT r.id, r.observed_at, json_extract(r.payload_json,'$.clock_status'), p.id,"
        " p.run_id, p.payload_json FROM forecast_receipts r JOIN discovery_passes p"
        " ON p.id=r.discovery_id WHERE r.subject_kind='discovery_pass' AND"
        + where
        + " ORDER BY r.id",
        params,
    ):
        receipt_id, observed, clock_status, pass_id, run_id, payload_json = row
        if clock_status != "trusted":
            diagnostics["untrusted_receipts_ignored"] += 1
            continue
        observed_at = datetime.fromisoformat(observed)
        if observed_at > cutoff:
            diagnostics["passes_first_witnessed_after_cutoff"] += 1
            continue
        if any(p["pass_id"] == pass_id for p in passes):
            continue  # the first receipt for a subject is the permanent one
        payload = json.loads(payload_json)
        manifest = payload["manifest"]
        through = manifest.get("fetch_seq_through")
        through = through if type(through) is int else -1
        passes.append(
            {
                "pass_id": pass_id,
                "run_id": run_id,
                "payload": payload,
                "observed_at": observed_at,
                "receipt_id": receipt_id,
                "through": through,
            }
        )
    diagnostics["passes_witnessed_by_cutoff"] = len(passes)
    witnessed_through = {}
    for entry in passes:
        witnessed_through[entry["run_id"]] = max(
            witnessed_through.get(entry["run_id"], -1), entry["through"]
        )
    # Orphans: witnessed discovery-stage fetches (never schedule or book, which Phase B
    # may also request) of a two-phase run, beyond every witnessed pass of that run.
    orphans = {}
    for row in db.execute(
        "SELECT r.id, r.observed_at, json_extract(r.payload_json,'$.clock_status'), f.seq,"
        " q.run_id FROM forecast_receipts r JOIN fetches f ON f.id=r.fetch_id"
        " JOIN requests q ON q.id=f.request_id WHERE r.subject_kind='fetch' AND q.stage IN"
        " ('status','series','document','markets','event') AND" + where + " ORDER BY r.id",
        params,
    ):
        receipt_id, observed, clock_status, seq, run_id = row
        if clock_status != "trusted" or datetime.fromisoformat(observed) > cutoff:
            continue
        run = _run_facts(db, run_id, runs)
        if run is None or run["protocol"] != COLLECTION_PROTOCOL:
            continue
        if seq > witnessed_through.get(run_id, -1) and seq > orphans.get(run_id, (-1,))[0]:
            orphans[run_id] = (seq, receipt_id, datetime.fromisoformat(observed))
    diagnostics["orphan_runs"] = len(orphans)
    candidates = [(entry["through"], 1, entry["pass_id"], "pass", entry) for entry in passes]
    candidates.extend(
        (seq, 0, 0, "orphan", (run_id, receipt_id, observed))
        for run_id, (seq, receipt_id, observed) in orphans.items()
    )
    if not candidates:
        return DiscoveryEvidence(
            cutoff,
            None,
            None,
            None,
            False,
            ("discovery_missing",),
            None,
            None,
            None,
            None,
            diagnostics,
        )
    _, _, _, kind, record = max(candidates, key=lambda c: c[:3])
    if kind == "orphan":
        run_id, receipt_id, observed = record
        return DiscoveryEvidence(
            cutoff,
            "orphan",
            None,
            run_id,
            False,
            ("discovery_summary_missing",),
            None,
            observed,
            receipt_id,
            None,
            diagnostics,
        )
    payload = record["payload"]
    run = _run_facts(db, record["run_id"], runs)
    if not payload["complete"]:
        reasons = tuple(payload["reasons"])
        complete = False
    else:
        reasons = verify_pass(payload, run)
        complete = not reasons
    return DiscoveryEvidence(
        cutoff,
        "pass",
        record["pass_id"],
        record["run_id"],
        complete,
        reasons,
        datetime.fromisoformat(payload["completed_at"]),
        record["observed_at"],
        record["receipt_id"],
        payload["config_hash"],
        diagnostics,
    )
