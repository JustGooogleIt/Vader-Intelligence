"""The finite forecast tick and inspection (specification §7, §2).

One invocation, in the specified order: witness prior completed evidence and discovery
summaries; prepare and witness identity bindings; compute due cutoff decisions from
evidence available at C; attempt operational publication separately; record missed
opportunities; exit. It adds no provider request, daemon or scheduler. Receipts are
processed before sources are chosen, and a receipt made now never authorizes an input
for an earlier C (the projection admits only receipts observed at or before C).

Publication is attempted for every eligible decision of the cohort that has no
publication record yet, at this tick's actual time: a decision whose publication
never committed (crash between the two records) is published by a later tick at that
later time, which is `allowed` inside the window or `missed` after it. It is never
backdated and never a more favorable time. Immediately before an allowed record is
committed, the latest already-available operational evidence for the contract is
checked: a post-cutoff eligibility decision showing the game no longer pregame or the
market closed vetoes publication with that evidence; it never changes the cutoff
decision (specification §2, "three independent decisions").
"""

import json
import time
import uuid
from datetime import datetime

from ..provenance import RunProvenanceV1, source_hash
from ..storage import timestamp, utc_now
from . import publication, service
from .policy import utc
from .references import reference
from .service import CONTRACT_REASONS, PREGAME_REASONS
from .witness import witness

VETO_REASONS = PREGAME_REASONS | CONTRACT_REASONS


def _resolve(clock, monotonic):
    injected = clock is not None or monotonic is not None
    return (clock or utc_now), (monotonic or time.monotonic), injected


def _decision_exists(db, protocol, mode, game_id, dataset=None):
    return (
        db.execute(
            "SELECT 1 FROM forecast_decisions WHERE json_extract(payload_json,'$.game_id')=?"
            " AND json_extract(payload_json,'$.dataset')=?"
            " AND json_extract(payload_json,'$.protocol')=?"
            " AND json_extract(payload_json,'$.config_hash')=?"
            " AND json_extract(payload_json,'$.mode')=?"
            " AND json_extract(payload_json,'$.horizon')=3600 LIMIT 1",
            (
                game_id,
                protocol.dataset if dataset is None else dataset,
                protocol.protocol_id,
                protocol.config_hash,
                mode,
            ),
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
        RunProvenanceV1(
            run_id,
            "forecast-tick",
            code_hash,
            {"protocol": protocol.config_hash},
            tool_configuration={"source_lineage": "synthetic" if injected else "live"},
        ),
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
        "publications_pending_before": 0,
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
            else:
                summary["decisions_abstained"] += 1
        # Phase 5: publication is a separate record for every eligible decision of this
        # cohort that has none, attempted at this tick's actual time.
        pending = _unpublished_decisions(db, protocol, mode)
        summary["publications_pending_before"] = len(pending)
        for decision_id, decision_digest, payload in pending:
            if monotonic() >= deadline:
                status = "partial"
                break
            try:
                verdict = _publish(
                    store,
                    decision_id,
                    decision_digest,
                    payload,
                    now,
                    run_id,
                    code_hash,
                    mode,
                    protocol,
                    clock,
                )
            except (ValueError, KeyError, TypeError) as exc:
                summary["errors"].append(f"publish {decision_id}: {str(exc)[:200]}")
                continue
            summary["publications"][verdict] += 1
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
        " AND json_extract(r.payload_json,'$.mode')=?"
        " AND EXISTS (SELECT 1 FROM runs w WHERE w.id=r.run_id AND w.kind='forecast-witness'))"
        " ORDER BY p.id",
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


def _unpublished_decisions(db, protocol, mode):
    """Eligible decisions of this cohort with no publication record, oldest first."""
    rows = db.execute(
        "SELECT d.id, d.digest, d.payload_json FROM forecast_decisions d"
        " WHERE d.mode=? AND d.protocol=? AND json_extract(d.payload_json,'$.config_hash')=?"
        " AND json_extract(d.payload_json,'$.dataset')=?"
        " AND json_extract(d.payload_json,'$.cutoff_eligible')=1"
        " AND NOT EXISTS (SELECT 1 FROM forecast_publications p WHERE p.decision_id=d.id)"
        " ORDER BY d.id",
        (mode, protocol.protocol_id, protocol.config_hash, protocol.dataset),
    ).fetchall()
    return [(row[0], row[1], json.loads(row[2])) for row in rows]


def operational_veto(db, payload, now):
    """Latest already-available post-cutoff evidence for the selected contract.

    Reads the newest committed eligibility decision for the contract made after the one
    the cutoff decision renewed on (it must already be in the archive: no request is
    made). A decision showing the game not pregame or the market no longer open vetoes
    publication; the veto carries that row's observation time and its typed schedule
    and event references. Returns None when no such evidence exists.
    """
    renewed = (payload["manifest"].get("renewal") or {}).get("eligibility_id") or 0
    row = db.execute(
        "SELECT id, eligible, reason, checked_at, schedule_fetch_id, market_fetch_id"
        " FROM eligibility WHERE ticker=? AND id>? AND checked_at<=? ORDER BY id DESC LIMIT 1",
        (payload["ticker"], renewed, now.isoformat()),
    ).fetchone()
    if row is None:
        return None
    eligibility_id, eligible, reason, checked_at, schedule_fetch_id, market_fetch_id = row
    if eligible or reason not in VETO_REASONS:
        return None
    references = []
    for fetch_id in (schedule_fetch_id, market_fetch_id):
        try:
            references.append(reference(db, "fetches", {"id": fetch_id}))
        except ValueError:
            pass  # a pending/absent attempt cannot be a typed reference; the row still vetoes
    return {
        "reason": reason,
        "observed_at": utc(datetime.fromisoformat(checked_at)),
        "eligibility_id": eligibility_id,
        "references": references,
    }


def _publish(
    store, decision_id, decision_digest, payload, now, run_id, code_hash, mode, protocol, clock
):
    db = store.db
    scheduled_start = utc(datetime.fromisoformat(payload["scheduled_start"]))
    veto = operational_veto(db, payload, now)
    classification, verdict = publication.decide(
        scheduled_start=scheduled_start,
        attempted_at=now,
        published_at=None,
        veto_observed_at=None if veto is None else veto["observed_at"],
        veto_reason=None if veto is None else veto["reason"],
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
    extra = {"decision_created_at": payload["created_at"]}
    references = ()
    if veto is not None:
        extra["veto"] = {
            "reason": veto["reason"],
            "observed_at": veto["observed_at"].isoformat(),
            "eligibility_id": veto["eligibility_id"],
        }
        if verdict == "vetoed":
            references = veto["references"]
    envelope = publication.envelope(
        common,
        decision_id=decision_id,
        decision_digest=decision_digest,
        attempted_at=timestamp(now),
        classification=classification,
        verdict=verdict,
        publication_policy=protocol.publication_policy,
        references=references,
        extra=extra,
    )
    store.append_forecast("forecast_publications", envelope)
    return verdict


def inspect(store, *, limit=20, after=None, namespace="local-witness-v1", dataset=None):
    """Read-only: recent decisions with the evidence and policy that produced them.

    Keyset pagination by decision id descending. Max limit 100 (specification §7).
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer in [1, 100]")
    db = store.db
    clauses, params = [], []
    if after:
        clauses.append("id < ?")
        params.append(after)
    if dataset:
        clauses.append("json_extract(payload_json,'$.dataset') = ?")
        params.append(dataset)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.execute(
        f"SELECT id, digest, payload_json FROM forecast_decisions {where} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    records = []
    for decision_id, digest, payload_json in rows:
        p = json.loads(payload_json)
        manifest = p["manifest"]
        publication_row = db.execute(
            "SELECT id, json_extract(payload_json,'$.verdict'),"
            " json_extract(payload_json,'$.manifest.policy_status'),"
            " json_extract(payload_json,'$.attempted_at'),"
            " json_extract(payload_json,'$.reasons')"
            " FROM forecast_publications WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        publication_record = None
        if publication_row is not None:
            receipt = db.execute(
                "SELECT r.id, r.observed_at, json_extract(r.payload_json,'$.clock_status')"
                " FROM forecast_receipts r WHERE r.publication_id=?"
                " AND json_extract(r.payload_json,'$.contract_version')=2"
                " AND json_extract(r.payload_json,'$.observer_namespace')=?"
                " AND EXISTS (SELECT 1 FROM runs w WHERE w.id=r.run_id AND w.kind='forecast-witness')"
                " ORDER BY r.id LIMIT 1",
                (publication_row[0], namespace),
            ).fetchone()
            publication_record = {
                "id": publication_row[0],
                "verdict": publication_row[1],
                "status": publication_row[2],
                "attempted_at": publication_row[3],
                "reasons": json.loads(publication_row[4]),
                "witnessed_at": None if receipt is None else receipt[1],
                "receipt_clock": None if receipt is None else receipt[2],
            }
        records.append(
            {
                "decision_id": decision_id,
                "digest": digest,
                "game_id": p["game_id"],
                "cutoff": p["cutoff"],
                "scheduled_start": p["scheduled_start"],
                "created_at": p["created_at"],
                "protocol": p["protocol"],
                "dataset": p["dataset"],
                "config_hash": p["config_hash"],
                "mode": p["mode"],
                "cutoff_eligible": p["cutoff_eligible"],
                "reasons": p["reasons"],
                "selected_ticker": manifest.get("selected_ticker"),
                "discovery": manifest.get("discovery"),
                "considered": {
                    "binding_id": p["binding_id"],
                    "binding_receipt_id": manifest.get("binding_receipt_id"),
                    "discovery_receipt_id": manifest.get("discovery_receipt_id"),
                    "renewal": manifest.get("renewal"),
                    "common_reasons": manifest.get("common_reasons"),
                    "detail_reasons": manifest.get("detail_reasons"),
                    "book_reasons": manifest.get("book_reasons"),
                },
                "results": {
                    name: {"probability": r["probability"], "reasons": r["reasons"]}
                    for name, r in p["results"].items()
                },
                "publication": publication_record,
            }
        )
    return {
        "namespace": namespace,
        "count": len(records),
        "next_before": records[-1]["decision_id"] if len(records) == limit else None,
        "decisions": records,
    }
