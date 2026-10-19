"""The finite forecast tick and inspection (specification §7, §2).

One invocation, in the specified order: witness prior completed evidence and discovery
summaries; prepare and witness identity bindings; compute due cutoff decisions from
evidence available at C; attempt operational publication separately; record missed
opportunities; exit. It adds no provider request, daemon or scheduler. Receipts are
processed before sources are chosen, and a receipt made now never authorizes an input
for an earlier C (the projection admits only receipts observed at or before C).
"""

import json
import time
import uuid
from datetime import datetime, timedelta

from ..provenance import RunProvenanceV1, source_hash
from ..storage import timestamp, utc_now
from . import publication, service
from .policy import utc
from .witness import witness


def _resolve(clock, monotonic):
    injected = clock is not None or monotonic is not None
    return (clock or utc_now), (monotonic or time.monotonic), injected


def _decision_exists(db, protocol, mode, game_id):
    return (
        db.execute(
            "SELECT 1 FROM forecast_decisions WHERE json_extract(payload_json,'$.game_id')=?"
            " AND json_extract(payload_json,'$.dataset')=?"
            " AND json_extract(payload_json,'$.protocol')=?"
            " AND json_extract(payload_json,'$.config_hash')=?"
            " AND json_extract(payload_json,'$.mode')=?"
            " AND json_extract(payload_json,'$.horizon')=3600 LIMIT 1",
            (game_id, protocol.dataset, protocol.protocol_id, protocol.config_hash, mode),
        ).fetchone()
        is not None
    )


def _binding_for(db, protocol, mode, game_id):
    return db.execute(
        "SELECT id FROM forecast_bindings WHERE candidate_key=? AND policy_version=?"
        " AND config_hash=? AND mode=? ORDER BY revision DESC LIMIT 1",
        (f"{game_id}", protocol.policy_version, protocol.config_hash, mode),
    ).fetchone()


def tick(store, protocol, *, clock=None, monotonic=None, budget=30.0):
    """Run one tick. Returns a summary dict; the CLI maps status to an exit code."""
    inject_clock, inject_monotonic = clock, monotonic  # forwarded to witness verbatim
    clock, monotonic, injected = _resolve(clock, monotonic)
    mode = "synthetic" if injected else "forward_shadow"
    namespace = protocol.observer_namespace
    if not isinstance(budget, (int, float)) or not 0 < budget <= 600:
        raise ValueError("budget must be in (0, 600] seconds")
    if store.db.in_transaction:
        raise ValueError("tick requires an idle connection")
    if store.schema_version() < 4:
        raise ValueError(
            "forecast tick requires record contract 2: vader forecast migrate --record-contract 2"
        )
    db = store.db
    deadline = monotonic() + budget
    run_id = str(uuid.uuid4())
    code_hash = source_hash()
    store.start_run(
        RunProvenanceV1(run_id, "forecast-tick", code_hash, {"protocol": protocol.config_hash}),
        timestamp(clock()),
    )
    summary = {
        "run_id": run_id,
        "protocol": protocol.protocol_id,
        "config_hash": protocol.config_hash,
        "dataset": protocol.dataset,
        "mode": mode,
        "bindings_prepared": 0,
        "bindings_skipped": {},
        "decisions_eligible": 0,
        "decisions_abstained": 0,
        "due_opportunities": 0,
        "publications": {"allowed": 0, "missed": 0, "vetoed": 0, "failed": 0},
        "witness": {},
        "errors": [],
    }
    status = "complete"
    try:
        # Phase 1: witness prior completed fetches and discovery summaries.
        summary["witness"]["pre"] = witness(
            store,
            clock=inject_clock,
            monotonic=inject_monotonic,
            budget=max(1.0, budget / 3),
            namespace=namespace,
        )
        # Phase 2: prepare bindings for games in complete, witnessed passes lacking one.
        for pass_id, game_id in _bindable_games(db, namespace, mode):
            if monotonic() >= deadline:
                status = "partial"
                break
            if _binding_for(db, protocol, mode, game_id):
                continue
            try:
                service.prepare_binding(
                    store,
                    pass_id=pass_id,
                    game_id=game_id,
                    protocol=protocol,
                    run_id=run_id,
                    code_hash=code_hash,
                    mode=mode,
                    created_at=timestamp(clock()),
                )
                summary["bindings_prepared"] += 1
            except service.BindingUnavailable as exc:
                summary["bindings_skipped"][exc.reason] = (
                    summary["bindings_skipped"].get(exc.reason, 0) + 1
                )
        # Phase 3: witness the just-prepared bindings (and any prior publications).
        summary["witness"]["bindings"] = witness(
            store,
            clock=inject_clock,
            monotonic=inject_monotonic,
            budget=max(1.0, budget / 3),
            namespace=namespace,
        )
        # Phase 4: compute due decisions (cutoff <= now) lacking one; abstain otherwise.
        now = utc(clock())
        for binding_id, game_id, cutoff in _due_bindings(db, protocol, mode, now):
            if monotonic() >= deadline:
                status = "partial"
                break
            summary["due_opportunities"] += 1
            if _decision_exists(db, protocol, mode, game_id):
                continue
            try:
                result = service.decide_opportunity(
                    store,
                    binding_id=binding_id,
                    protocol=protocol,
                    run_id=run_id,
                    code_hash=code_hash,
                    mode=mode,
                    created_at=timestamp(clock()),
                    namespace=namespace,
                )
            except (ValueError, KeyError, TypeError) as exc:
                summary["errors"].append(f"decide {game_id}: {str(exc)[:200]}")
                continue
            if result.cutoff_eligible:
                summary["decisions_eligible"] += 1
                # Phase 5: publication is a separate record, attempted now.
                verdict = _publish(
                    store, result, cutoff, now, run_id, code_hash, mode, protocol, clock
                )
                summary["publications"][verdict] += 1
            else:
                summary["decisions_abstained"] += 1
        # Phase 6: witness publications so a later tick can confirm timeliness.
        summary["witness"]["publications"] = witness(
            store,
            clock=inject_clock,
            monotonic=inject_monotonic,
            budget=max(1.0, budget / 3),
            namespace=namespace,
        )
    except BaseException as exc:
        summary["errors"].append(str(exc)[:200])
        store.finish_run(run_id, "interrupted", summary | {"status": "interrupted"})
        raise
    if summary["errors"] and status == "complete":
        status = "partial"
    if (
        status == "complete"
        and summary["decisions_eligible"] == 0
        and summary["decisions_abstained"] == 0
    ):
        status = "inconclusive"  # no due decisions this tick
    summary["status"] = status
    store.finish_run(run_id, status, summary)
    return summary


def _bindable_games(db, namespace, mode):
    """(pass_id, game_id) for games with an eligible contract in a complete pass that has
    a trusted contract-2 witness receipt in this namespace and mode."""
    rows = db.execute(
        "SELECT p.id, p.run_id, p.payload_json FROM discovery_passes p"
        " WHERE json_extract(p.payload_json,'$.complete')=1"
        " AND json_extract(p.payload_json,'$.mode')=?"
        " AND EXISTS (SELECT 1 FROM forecast_receipts r WHERE r.discovery_id=p.id"
        " AND json_extract(r.payload_json,'$.contract_version')=2"
        " AND json_extract(r.payload_json,'$.observer_namespace')=?"
        " AND json_extract(r.payload_json,'$.clock_status')='trusted'"
        " AND json_extract(r.payload_json,'$.mode')=?) ORDER BY p.id",
        (mode, namespace, mode),
    ).fetchall()
    out = []
    for pass_id, run_id, payload_json in rows:
        payload = json.loads(payload_json)
        games = db.execute(
            "SELECT DISTINCT game_id FROM eligibility WHERE run_id=? AND id>? AND id<=?"
            " AND eligible=1 AND game_id IS NOT NULL ORDER BY game_id",
            (run_id, payload["eligibility_after_id"], payload["eligibility_through_id"]),
        ).fetchall()
        for (game_id,) in games:
            out.append((pass_id, game_id))
    return out


def _due_bindings(db, protocol, mode, now):
    rows = db.execute(
        "SELECT id, json_extract(payload_json,'$.game_id'), json_extract(payload_json,'$.cutoff')"
        " FROM forecast_bindings WHERE policy_version=? AND config_hash=? AND mode=?"
        " ORDER BY id",
        (protocol.policy_version, protocol.config_hash, mode),
    ).fetchall()
    due = []
    for binding_id, game_id, cutoff in rows:
        if utc(datetime.fromisoformat(cutoff)) <= now:
            due.append((binding_id, game_id, utc(datetime.fromisoformat(cutoff))))
    return due


def _publish(store, result, cutoff, now, run_id, code_hash, mode, protocol, clock):
    scheduled_start = cutoff + timedelta(seconds=3600)
    classification, verdict = publication.decide(
        scheduled_start=scheduled_start,
        attempted_at=now,
        published_at=None,
        clock_trusted=True,
        mode=mode,
    )
    common = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": timestamp(clock()),
        "code_hash": code_hash,
        "config_hash": protocol.config_hash,
        "mode": mode,
    }
    envelope = publication.envelope(
        common,
        decision_id=result.decision_id,
        decision_digest=result.digest,
        attempted_at=timestamp(now),
        classification=classification,
        verdict=verdict,
        publication_policy=protocol.publication_policy,
    )
    store.append_forecast("forecast_publications", envelope)
    return verdict


def inspect(store, *, limit=20, after=None, namespace="local-witness-v1"):
    """Read-only: recent decisions with the evidence and policy that produced them.

    Keyset pagination by decision id descending. Max limit 100 (specification §7).
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer in [1, 100]")
    db = store.db
    clause = "WHERE id < ?" if after else ""
    params = ((after,) if after else ()) + (limit,)
    rows = db.execute(
        f"SELECT id, digest, payload_json FROM forecast_decisions {clause}"
        " ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    records = []
    for decision_id, digest, payload_json in rows:
        p = json.loads(payload_json)
        manifest = p["manifest"]
        publication_row = db.execute(
            "SELECT json_extract(payload_json,'$.verdict'),"
            " json_extract(payload_json,'$.manifest.policy_status')"
            " FROM forecast_publications WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        records.append(
            {
                "decision_id": decision_id,
                "digest": digest,
                "game_id": p["game_id"],
                "cutoff": p["cutoff"],
                "protocol": p["protocol"],
                "dataset": p["dataset"],
                "config_hash": p["config_hash"],
                "mode": p["mode"],
                "cutoff_eligible": p["cutoff_eligible"],
                "reasons": p["reasons"],
                "selected_ticker": manifest.get("selected_ticker"),
                "discovery": manifest.get("discovery"),
                "considered": {
                    "binding_receipt_id": manifest.get("binding_receipt_id"),
                    "discovery_receipt_id": manifest.get("discovery_receipt_id"),
                    "common_reasons": manifest.get("common_reasons"),
                    "book_reasons": manifest.get("book_reasons"),
                },
                "results": {
                    name: {"probability": r["probability"], "reasons": r["reasons"]}
                    for name, r in p["results"].items()
                },
                "publication": None
                if publication_row is None
                else {"verdict": publication_row[0], "status": publication_row[1]},
            }
        )
    return {
        "namespace": namespace,
        "count": len(records),
        "next_before": records[-1]["decision_id"] if len(records) == limit else None,
        "decisions": records,
    }
