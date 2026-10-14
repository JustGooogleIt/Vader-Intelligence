"""Prepare identity bindings and generate cutoff decisions (specification §3, §4, §6).

This is the persistence-and-orchestration layer for the forecast tick and the
archive-only reconstruction. It reuses the pure policy (`cutoff_eligibility`,
baselines) and the read-side projection; it never calls a provider, reads no wall
clock of its own (callers supply times), and writes only through the journal. A
decision carries enough provenance in its manifest to recompute its result from the
same persisted inputs.

At the cutoff C the common predicates are established from evidence, never asserted:

- identity: the binding was witnessed by C and the applicable discovery pass at C
  (the most recent complete pass witnessed by C) still lists the selected contract
  under the same event with the same game and YES team;
- pregame: that pass's eligibility decision for the selected contract is eligible
  (official schedule Preview/Scheduled, no reschedule flags, rule start equal to the
  schedule start) and the start equals the binding's start (no revision);
- contract validity: the same decision found the market open with the $1 notional
  and a recognized full-game rule.

The schedule and event evidence behind that decision are the renewal inputs at C:
both must be retrieved and witnessed by C within the 300-second limit. A binding
prepared hours earlier is therefore never scored on stale evidence.
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

# scope.eligible rejection reasons by the common predicate they fail (specification §3,
# §4). An unknown reason fails every predicate closed.
IDENTITY_REASONS = frozenset(
    {
        "unknown_team_alias",
        "invalid_participants",
        "ambiguous_contract_family",
        "winner_label_mismatch",
        "unmapped_game",
        "ambiguous_game",
        "doubleheader_requires_game_number",
    }
)
PREGAME_REASONS = frozenset(
    {
        "schedule_stale",
        "rescheduled_game",
        "not_scheduled_pregame",
        "start_time_unknown",
        "schedule_rule_time_mismatch",
        "unsupported_game_type",
        "past_pregame_cutoff",
    }
)
CONTRACT_REASONS = frozenset(
    {
        "unsupported_event_scope",
        "unsupported_market_scope",
        "market_not_open",
        "unsupported_notional",
        "unknown_notional",
        "unrecognized_full_game_rule",
        "invalid_rule_time",
        "rule_timezone_mismatch",
    }
)
KNOWN_REASONS = IDENTITY_REASONS | PREGAME_REASONS | CONTRACT_REASONS | {"eligible"}


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


@dataclass(frozen=True)
class Renewal:
    """The applicable pass's evidence for the selected contract at C."""

    eligibility_id: int | None
    identity: bool
    pregame: bool
    contract_valid: bool
    details: tuple
    schedule_fetch_id: str | None
    market_fetch_id: str | None
    reason: str | None


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
    if fetch_id is None:
        return None, None
    availability = evidence.fetch_availability(db, fetch_id, namespace=namespace, mode=mode)
    if availability.retrieved_at is None:
        return None, availability
    return EvidenceTimes(availability.retrieved_at, availability.observed_at), availability


def renewal_at_cutoff(db, projection, binding):
    """Establish identity, pregame and contract validity for the selected contract from
    the applicable pass at C. Returns a Renewal; every predicate is False when the pass
    does not apply, so the pure policy reports the failure in its fixed reason order."""
    if projection.kind != "pass":
        return Renewal(None, False, False, False, ("discovery_not_applicable",), None, None, None)
    pass_payload = json.loads(
        db.execute(
            "SELECT payload_json FROM discovery_passes WHERE id=?", (projection.pass_id,)
        ).fetchone()[0]
    )
    manifest = pass_payload["manifest"]
    details = []
    identity = pregame = contract_valid = True
    if binding["game_id"] not in manifest.get("official_game_ids", []):
        details.append("schedule_missing_game")
        pregame = False
    candidate = next(
        (c for c in manifest.get("candidates", []) if c.get("ticker") == binding["ticker"]), None
    )
    if candidate is None:
        details.append("contract_missing_from_discovery")
        identity = contract_valid = False
    elif candidate.get("event_ticker") != binding["event_ticker"]:
        details.append("event_listing_changed")
        identity = False
    row = db.execute(
        "SELECT id, eligible, reason, data_json, schedule_fetch_id, market_fetch_id FROM eligibility"
        " WHERE run_id=? AND id>? AND id<=? AND ticker=? ORDER BY id DESC LIMIT 1",
        (
            pass_payload["run_id"],
            pass_payload["eligibility_after_id"],
            pass_payload["eligibility_through_id"],
            binding["ticker"],
        ),
    ).fetchone()
    if row is None:
        details.append("eligibility_row_missing")
        return Renewal(None, False, False, False, tuple(details), None, None, None)
    eligibility_id, eligible, reason, data_json, schedule_fetch_id, market_fetch_id = row
    data = json.loads(data_json)
    if eligible:
        if (
            data.get("game_id") != binding["game_id"]
            or data.get("team_id") != binding["yes_team_id"]
        ):
            details.append("identity_changed")
            identity = False
        start = data.get("scheduled_start")
        if start is None or utc(datetime.fromisoformat(start)) != utc(
            datetime.fromisoformat(binding["scheduled_start"])
        ):
            details.append("schedule_revised")
            pregame = False
    else:
        details.append(reason if reason in KNOWN_REASONS else "unknown_eligibility_reason")
        if reason not in KNOWN_REASONS:
            identity = pregame = contract_valid = False
        identity = identity and reason not in IDENTITY_REASONS
        pregame = pregame and reason not in PREGAME_REASONS
        contract_valid = contract_valid and reason not in CONTRACT_REASONS
    return Renewal(
        eligibility_id,
        identity,
        pregame,
        contract_valid,
        tuple(details),
        schedule_fetch_id,
        market_fetch_id,
        reason,
    )


@dataclass(frozen=True)
class CutoffFacts:
    """Everything the cutoff decision froze, computed read-only from the archive."""

    cutoff: object
    evidence_mode: str
    projection: object
    renewal: Renewal
    binding_avail: object
    schedule_avail: object
    market_avail: object
    common: object
    reasons: tuple
    inputs: object
    midpoint_probability: str | None
    midpoint_reasons: tuple
    midpoint_inputs: dict

    @property
    def cutoff_eligible(self):
        return self.common.eligible


def cutoff_facts(db, *, binding, namespace, evidence_mode):
    """Read-only computation of the common eligibility, renewal and midpoint input for a
    binding at its cutoff. Used by the decision writer and by evaluation annotations of
    unattempted opportunities; writes nothing."""
    game_id = binding["game_id"]
    ticker = binding["ticker"]
    cutoff = utc(datetime.fromisoformat(binding["cutoff"]))
    projection = evidence.discovery_as_of(db, cutoff, namespace=namespace, mode=evidence_mode)
    renewal = renewal_at_cutoff(db, projection, binding)
    schedule_times, schedule_avail = _availability_times(
        db, renewal.schedule_fetch_id, namespace, evidence_mode
    )
    market_times, market_avail = _availability_times(
        db, renewal.market_fetch_id, namespace, evidence_mode
    )
    binding_avail = evidence.binding_availability(
        db, binding["id"], namespace=namespace, mode=evidence_mode
    )
    untrusted = (
        any(a is not None and a.status == "clock_untrusted" for a in (schedule_avail, market_avail))
        or binding_avail.status == "clock_untrusted"
    )
    common = cutoff_eligibility(
        cutoff=cutoff,
        binding_observed_at=binding_avail.observed_at,
        discovery=projection.times,
        schedule=schedule_times,
        market=market_times,
        universe_complete=projection.universe_complete,
        identity_proven=binding_avail.status == "witnessed" and renewal.identity,
        pregame=renewal.pregame,
        contract_valid=renewal.contract_valid,
        clock_trusted=not untrusted,
    )
    # All applicable reasons: the policy's fixed-order primary reasons first, then the
    # evidence-level details that explain them (specification §4).
    reasons = list(common.reasons)
    reasons.extend(d for d in renewal.details if d not in reasons)
    inputs = select_inputs(
        db,
        ticker=ticker,
        game_id=game_id,
        cutoff=cutoff,
        namespace=namespace,
        mode=evidence_mode,
        scheduled_start=binding["scheduled_start"],
    )
    midpoint_probability = None
    midpoint_reasons = list(reasons)
    midpoint_inputs = {}
    if common.eligible:
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
    return CutoffFacts(
        cutoff,
        evidence_mode,
        projection,
        renewal,
        binding_avail,
        schedule_avail,
        market_avail,
        common,
        tuple(reasons),
        inputs,
        midpoint_probability,
        tuple(midpoint_reasons),
        midpoint_inputs,
    )


def load_binding(db, binding_id):
    row = db.execute(
        "SELECT payload_json FROM forecast_bindings WHERE id=?", (binding_id,)
    ).fetchone()
    if row is None:
        raise ValueError("missing binding")
    return json.loads(row[0]) | {"id": binding_id}


def decide_opportunity(
    store,
    *,
    binding_id,
    protocol,
    run_id,
    code_hash,
    mode,
    created_at,
    namespace,
    dataset=None,
    source_ceiling=None,
):
    """Compute and persist the cutoff decision for one prepared binding at its C.

    Reads only evidence available (retrieved and witnessed) at the binding's cutoff.
    `mode` is the decision's cohort mode; a historical reconstruction reads the forward
    receipts of the live evidence it cites (plan §9.3 M9/M10). Returns a DecisionResult.
    Idempotent: the decision key excludes time and ticker, so a repeat returns the
    existing decision.
    """
    db = store.db
    dataset = protocol.dataset if dataset is None else dataset
    evidence_mode = "forward_shadow" if mode == "historical_reconstruction" else mode
    binding = load_binding(db, binding_id)
    facts = cutoff_facts(db, binding=binding, namespace=namespace, evidence_mode=evidence_mode)
    projection, renewal, inputs = facts.projection, facts.renewal, facts.inputs
    cutoff_eligible = facts.cutoff_eligible
    reasons = list(facts.reasons)
    if cutoff_eligible:
        constant_result = {"probability": "0.5", "reasons": [], "inputs": {"value": "0.5"}}
    else:
        constant_result = {"probability": None, "reasons": reasons, "inputs": {}}
    midpoint_result = {
        "probability": facts.midpoint_probability,
        "reasons": list(facts.midpoint_reasons),
        "inputs": facts.midpoint_inputs,
    }

    pass_row = (
        db.execute(
            "SELECT digest FROM discovery_passes WHERE id=?", (projection.pass_id,)
        ).fetchone()
        if projection.pass_id is not None
        else None
    )
    archive_ceiling = db.execute("SELECT COALESCE(MAX(seq),0) FROM fetches").fetchone()[0]
    if source_ceiling is None:
        source_ceiling = archive_ceiling
    elif type(source_ceiling) is not int or not 0 <= source_ceiling <= archive_ceiling:
        raise ValueError("source ceiling must be an integer within the archive")

    schedule_avail, market_avail, binding_avail = (
        facts.schedule_avail,
        facts.market_avail,
        facts.binding_avail,
    )
    manifest = {
        "selection_version": protocol.selection_version,
        "selected_ticker": binding["ticker"],
        "event_ticker": binding["event_ticker"],
        "alternatives": binding["manifest"].get("alternatives", []),
        "binding_receipt_id": binding_avail.receipt_id,
        "binding_availability": binding_avail.status,
        "discovery_receipt_id": projection.receipt_id,
        "discovery_kind": projection.kind,
        "evidence_mode": evidence_mode,
        "renewal": {
            "eligibility_id": renewal.eligibility_id,
            "reason": renewal.reason,
            "schedule_fetch_id": renewal.schedule_fetch_id,
            "market_fetch_id": renewal.market_fetch_id,
            "schedule_availability": None if schedule_avail is None else schedule_avail.status,
            "market_availability": None if market_avail is None else market_avail.status,
            "identity": renewal.identity,
            "pregame": renewal.pregame,
            "contract_valid": renewal.contract_valid,
        },
        "common_reasons": list(facts.common.reasons),
        "detail_reasons": list(renewal.details),
        "book_reasons": list(inputs.reasons),
    }
    references = []
    if projection.pass_id is not None:
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
        "dataset": dataset,
        "protocol": protocol.protocol_id,
        "game_id": binding["game_id"],
        "unresolved_key": None,
        # Identity and the considered pass are provenance of abstentions as well as of
        # forecasts: input IDs are preserved even for rejected inputs (specification §4).
        "ticker": binding["ticker"],
        "yes_team_id": binding["yes_team_id"],
        "horizon": HORIZON,
        "scheduled_start": binding["scheduled_start"],
        "cutoff": binding["cutoff"],
        "binding_id": binding_id,
        "discovery_id": projection.pass_id,
        "selection_version": protocol.selection_version,
        "baseline_versions": ["constant-v1", "midpoint-v1"],
        "cutoff_eligible": cutoff_eligible,
        "reasons": [] if cutoff_eligible else reasons,
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
        reasons=tuple(reasons),
        constant_probability=constant_result["probability"],
        midpoint_probability=facts.midpoint_probability,
        midpoint_reasons=tuple(facts.midpoint_reasons),
    )


def redecide(db, decision_id, namespace):
    """Diagnostic recompute of a persisted decision from the same persisted inputs.

    Writes nothing. Returns the recomputed facts next to the stored ones so a caller can
    assert replay reproduces the stored result. It reuses the binding's cutoff and the
    same projection/selection, so appended later evidence (including outcomes) cannot
    change it.
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
    binding = load_binding(db, binding_id)
    mode = stored["manifest"].get("evidence_mode") or stored["mode"]
    facts = cutoff_facts(db, binding=binding, namespace=namespace, evidence_mode=mode)
    return {
        "cutoff_eligible": facts.cutoff_eligible,
        "stored_cutoff_eligible": stored["cutoff_eligible"],
        "reasons": list(facts.reasons),
        "universe_complete": facts.projection.universe_complete,
        "renewal": {
            "identity": facts.renewal.identity,
            "pregame": facts.renewal.pregame,
            "contract_valid": facts.renewal.contract_valid,
            "details": list(facts.renewal.details),
        },
        "constant": constant() if facts.cutoff_eligible else None,
        "midpoint": facts.midpoint_probability,
        "stored_midpoint": stored["results"]["midpoint-v1"]["probability"],
        "stored_constant": stored["results"]["constant-v1"]["probability"],
    }
