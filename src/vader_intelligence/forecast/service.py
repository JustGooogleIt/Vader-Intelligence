"""Prepare identity bindings and generate cutoff decisions (specification §3, §4, §6).

This is the persistence-and-orchestration layer for the forecast tick. It reuses the
pure policy (`cutoff_eligibility`, baselines) and the read-side projection; it never
calls a provider, reads no wall clock of its own (the tick supplies times), and writes
only through the journal. A decision carries enough provenance in its manifest to
recompute its result from the same persisted inputs.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import evidence
from .baselines import InvalidBook, constant, midpoint
from .policy import EvidenceTimes, cutoff_eligibility, utc
from .references import reference
from .selection import Contract, select_contract, select_inputs

HORIZON = 3600
GAME_NUMBER = re.compile(r"G([12])$")


class BindingUnavailable(Exception):
    """A game has no provable pre-book identity in this pass; reason is a fixed label."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class DecisionResult:
    decision_id: int
    digest: str
    cutoff_eligible: bool
    reasons: tuple
    constant_probability: str | None
    midpoint_probability: str | None
    midpoint_reasons: tuple


def _eligibility_rows(db, pass_payload, game_id):
    run_id = pass_payload["run_id"]
    after = pass_payload["eligibility_after_id"]
    through = pass_payload["eligibility_through_id"]
    rows = db.execute(
        "SELECT id, ticker, game_id, eligible, data_json, schedule_fetch_id, market_fetch_id"
        " FROM eligibility WHERE run_id=? AND id>? AND id<=? AND game_id=? ORDER BY id",
        (run_id, after, through, game_id),
    ).fetchall()
    return [
        dict(
            zip(
                (
                    "id",
                    "ticker",
                    "game_id",
                    "eligible",
                    "data_json",
                    "schedule_fetch_id",
                    "market_fetch_id",
                ),
                r,
            )
        )
        for r in rows
    ]


def prepare_binding(store, *, pass_id, game_id, protocol, run_id, code_hash, mode, created_at):
    """Build and persist the single-revision identity binding for one game.

    Raises BindingUnavailable when the game's identity cannot be proven from this pass
    (not exactly two eligible contracts, selection ambiguous, or missing evidence). The
    binding is contract-1 (bindings have one contract); it is witnessed by the tick.
    """
    db = store.db
    # Idempotent per cohort: a binding already prepared for this game is reused as-is
    # (single revision; schedule-revision chains are a documented later increment).
    existing = db.execute(
        "SELECT id, digest, payload_json FROM forecast_bindings WHERE candidate_key=?"
        " AND policy_version=? AND config_hash=? AND mode=? ORDER BY revision DESC LIMIT 1",
        (f"{game_id}", protocol.policy_version, protocol.config_hash, mode),
    ).fetchone()
    if existing is not None:
        return existing[0], existing[1], json.loads(existing[2])
    pass_payload = json.loads(
        db.execute("SELECT payload_json FROM discovery_passes WHERE id=?", (pass_id,)).fetchone()[0]
    )
    manifest = pass_payload["manifest"]
    event_by_ticker = {c["ticker"]: c["event_ticker"] for c in manifest.get("candidates", [])}
    rows = _eligibility_rows(db, pass_payload, game_id)
    eligible_rows = [r for r in rows if r["eligible"]]
    if len(eligible_rows) != 2:
        raise BindingUnavailable("identity_not_two_contracts")
    candidates = []
    starts, numbers, schedule_ids, market_ids = set(), set(), set(), set()
    for row in eligible_rows:
        data = json.loads(row["data_json"])
        team = data.get("team_id")
        event_ticker = event_by_ticker.get(row["ticker"])
        if team is None or event_ticker is None or data.get("scheduled_start") is None:
            raise BindingUnavailable("identity_evidence_missing")
        candidates.append(
            {"event_ticker": event_ticker, "ticker": row["ticker"], "yes_team_id": team}
        )
        starts.add(data["scheduled_start"])
        match = GAME_NUMBER.search(event_ticker)
        numbers.add(int(match.group(1)) if match else None)
        schedule_ids.add(row["schedule_fetch_id"])
        market_ids.add(row["market_fetch_id"])
    if len(starts) != 1 or len(numbers) != 1:
        raise BindingUnavailable("identity_ambiguous_start")
    team_ids = sorted({c["yes_team_id"] for c in candidates})
    if len(team_ids) != 2:
        raise BindingUnavailable("identity_not_two_teams")
    selection = select_contract(candidates, team_ids)
    if selection.contract is None:
        raise BindingUnavailable(selection.reasons[0])
    chosen: Contract = selection.contract
    scheduled_start = utc(datetime.fromisoformat(starts.pop()))
    cutoff = scheduled_start - timedelta(seconds=HORIZON)
    documents = sorted(manifest.get("documents", []), key=lambda d: d["url"])
    if not documents:
        raise BindingUnavailable("identity_terms_missing")
    terms_digest = documents[0]["raw_sha256"]
    # Evidence tuple: the event, schedule and contract-terms fetches, re-validated on insert.
    schedule_fetch_id = sorted(schedule_ids)[0]
    market_fetch_id = sorted(market_ids)[0]
    references = [
        reference(db, "fetches", {"id": market_fetch_id}),
        reference(db, "fetches", {"id": schedule_fetch_id}),
        reference(db, "fetches", {"id": documents[0]["fetch_id"]}),
    ]
    game_number = numbers.pop()
    envelope = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": created_at,
        "code_hash": code_hash,
        "config_hash": protocol.config_hash,
        "mode": mode,
        "references": references,
        "candidate_key": f"{game_id}",
        "revision": 1,
        "previous_id": None,
        "policy_version": protocol.policy_version,
        "game_id": game_id,
        "event_ticker": chosen.event_ticker,
        "ticker": chosen.ticker,
        "yes_team_id": chosen.yes_team_id,
        "team_ids": team_ids,
        "original_date": scheduled_start.date().isoformat(),
        "original_start": scheduled_start.isoformat(),
        "game_number": game_number,
        "scheduled_start": scheduled_start.isoformat(),
        "cutoff": cutoff.isoformat(),
        "status": "prepared",
        "reasons": [],
        "manifest": {
            "source_pass_id": pass_id,
            "event_fetch_id": market_fetch_id,
            "schedule_fetch_id": schedule_fetch_id,
            "terms_fetch_id": documents[0]["fetch_id"],
            "alternatives": [c.ticker for c in selection.alternatives],
        },
        "terms_digest": terms_digest,
    }
    binding_id, binding_digest = store.append_forecast("forecast_bindings", envelope)
    return binding_id, binding_digest, envelope


def _availability_times(db, fetch_id, namespace, mode):
    availability = evidence.fetch_availability(db, fetch_id, namespace=namespace, mode=mode)
    if availability.retrieved_at is None:
        return None, availability
    return EvidenceTimes(availability.retrieved_at, availability.observed_at), availability


def decide_opportunity(
    store, *, binding_id, protocol, run_id, code_hash, mode, created_at, namespace
):
    """Compute and persist the cutoff decision for one prepared binding at its C.

    Reads only evidence available (retrieved and witnessed) at the binding's cutoff.
    Returns a DecisionResult. Idempotent: the decision key excludes time and ticker, so
    a repeat returns the existing decision.
    """
    db = store.db
    binding = json.loads(
        db.execute(
            "SELECT payload_json FROM forecast_bindings WHERE id=?", (binding_id,)
        ).fetchone()[0]
    )
    game_id = binding["game_id"]
    ticker = binding["ticker"]
    cutoff = utc(datetime.fromisoformat(binding["cutoff"]))
    bmanifest = binding["manifest"]

    projection = evidence.discovery_as_of(db, cutoff, namespace=namespace, mode=mode)
    schedule_times, schedule_avail = _availability_times(
        db, bmanifest["schedule_fetch_id"], namespace, mode
    )
    market_times, market_avail = _availability_times(
        db, bmanifest["event_fetch_id"], namespace, mode
    )
    binding_avail = evidence.binding_availability(db, binding_id, namespace=namespace, mode=mode)

    untrusted = (
        any(a.status == "clock_untrusted" for a in (schedule_avail, market_avail))
        or binding_avail.status == "clock_untrusted"
    )
    clock_trusted = not untrusted
    identity_proven = binding_avail.status == "witnessed" and projection.kind == "pass"
    pregame = projection.kind == "pass" and projection.universe_complete
    contract_valid = pregame

    common = cutoff_eligibility(
        cutoff=cutoff,
        binding_observed_at=binding_avail.observed_at,
        discovery=projection.times,
        schedule=schedule_times,
        market=market_times,
        universe_complete=projection.universe_complete,
        identity_proven=identity_proven,
        pregame=pregame,
        contract_valid=contract_valid,
        clock_trusted=clock_trusted,
    )
    cutoff_eligible = common.eligible

    inputs = select_inputs(
        db, ticker=ticker, game_id=game_id, cutoff=cutoff, namespace=namespace, mode=mode
    )
    midpoint_probability = None
    midpoint_reasons = list(common.reasons)
    midpoint_inputs = {}
    if cutoff_eligible:
        midpoint_reasons = []
        if inputs.eligible:
            try:
                value = midpoint(inputs.book.raw)
                midpoint_probability = format(value, "f")
                midpoint_inputs = {
                    "book_fetch_id": inputs.book.fetch_id,
                    "book_receipt_id": inputs.book.receipt_id,
                    "retrieved_at": inputs.book.retrieved_at.isoformat(),
                    "observed_at": inputs.book.observed_at.isoformat(),
                }
            except InvalidBook as exc:
                midpoint_reasons = [exc.reason]
        else:
            midpoint_reasons = list(inputs.reasons)

    if cutoff_eligible:
        constant_result = {"probability": "0.5", "reasons": [], "inputs": {"value": "0.5"}}
    else:
        constant_result = {"probability": None, "reasons": list(common.reasons), "inputs": {}}
    midpoint_result = {
        "probability": midpoint_probability,
        "reasons": midpoint_reasons,
        "inputs": midpoint_inputs,
    }

    pass_row = (
        db.execute(
            "SELECT digest FROM discovery_passes WHERE id=?", (projection.pass_id,)
        ).fetchone()
        if projection.pass_id is not None
        else None
    )
    source_ceiling = db.execute("SELECT COALESCE(MAX(seq),0) FROM fetches").fetchone()[0]

    manifest = {
        "selection_version": protocol.selection_version,
        "selected_ticker": ticker,
        "event_ticker": binding["event_ticker"],
        "alternatives": bmanifest.get("alternatives", []),
        "binding_receipt_id": binding_avail.receipt_id,
        "discovery_receipt_id": projection.receipt_id,
        "schedule_fetch_id": bmanifest["schedule_fetch_id"],
        "event_fetch_id": bmanifest["event_fetch_id"],
        "common_reasons": list(common.reasons),
        "book_reasons": list(inputs.reasons),
    }
    references = []
    if projection.pass_id is not None and cutoff_eligible:
        manifest["discovery"] = {
            "id": projection.pass_id,
            "digest": pass_row[0],
            "collector_config_hash": projection.collector_config_hash,
        }
    if inputs.book is not None:
        references.append(reference(db, "fetches", {"id": inputs.book.fetch_id}))

    envelope = {
        "schema_version": 1,
        "contract_version": 2,
        "run_id": run_id,
        "created_at": created_at,
        "code_hash": code_hash,
        "config_hash": protocol.config_hash,
        "mode": mode,
        "references": references,
        "dataset": protocol.dataset,
        "protocol": protocol.protocol_id,
        "game_id": game_id,
        "unresolved_key": None,
        "ticker": ticker if cutoff_eligible else None,
        "yes_team_id": binding["yes_team_id"] if cutoff_eligible else None,
        "horizon": HORIZON,
        "scheduled_start": binding["scheduled_start"] if cutoff_eligible else None,
        "cutoff": binding["cutoff"],
        "binding_id": binding_id if cutoff_eligible else None,
        "discovery_id": projection.pass_id if cutoff_eligible else None,
        "selection_version": protocol.selection_version,
        "baseline_versions": ["constant-v1", "midpoint-v1"],
        "cutoff_eligible": cutoff_eligible,
        "reasons": [] if cutoff_eligible else list(common.reasons),
        "results": {"constant-v1": constant_result, "midpoint-v1": midpoint_result},
        "manifest": manifest,
        "source_ceiling": source_ceiling,
        "supersedes_id": None,
    }
    decision_id, decision_digest = store.append_forecast("forecast_decisions", envelope)
    return DecisionResult(
        decision_id=decision_id,
        digest=decision_digest,
        cutoff_eligible=cutoff_eligible,
        reasons=tuple(common.reasons),
        constant_probability=constant_result["probability"],
        midpoint_probability=midpoint_probability,
        midpoint_reasons=tuple(midpoint_reasons),
    )


def redecide(db, decision_id, namespace):
    """Diagnostic recompute of a persisted decision from the same persisted inputs.

    Writes nothing. Returns the recomputed (cutoff_eligible, constant, midpoint)
    triple so a caller can assert replay reproduces the stored result. It reuses the
    binding's cutoff and the same projection/selection, so appended later evidence
    cannot change it.
    """
    row = db.execute(
        "SELECT payload_json FROM forecast_decisions WHERE id=?", (decision_id,)
    ).fetchone()
    if row is None:
        raise ValueError("missing decision")
    stored = json.loads(row[0])
    binding_id = stored["binding_id"]
    if binding_id is None:
        return stored["cutoff_eligible"], stored["results"]
    binding = json.loads(
        db.execute(
            "SELECT payload_json FROM forecast_bindings WHERE id=?", (binding_id,)
        ).fetchone()[0]
    )
    cutoff = utc(datetime.fromisoformat(binding["cutoff"]))
    mode = stored["mode"]
    projection = evidence.discovery_as_of(db, cutoff, namespace=namespace, mode=mode)
    inputs = select_inputs(
        db,
        ticker=binding["ticker"],
        game_id=binding["game_id"],
        cutoff=cutoff,
        namespace=namespace,
        mode=mode,
    )
    recomputed_midpoint = None
    if stored["cutoff_eligible"] and inputs.eligible:
        try:
            recomputed_midpoint = format(midpoint(inputs.book.raw), "f")
        except InvalidBook:
            recomputed_midpoint = None
    return {
        "cutoff_eligible": stored["cutoff_eligible"],
        "universe_complete": projection.universe_complete,
        "constant": constant() if stored["cutoff_eligible"] else None,
        "midpoint": recomputed_midpoint,
        "stored_midpoint": stored["results"]["midpoint-v1"]["probability"],
        "stored_constant": stored["results"]["constant-v1"]["probability"],
    }
