"""Evaluation materialization (F3.5; specification §5, §6, §7).

`evaluate` freezes one cohort's population, joins every opportunity to the archived
outcome view at an explicit `outcomes_as_of` retrieval-time boundary and fetch-sequence
ceiling, scores what the view allows, and writes one immutable evaluation run with its
items in a single transaction (no externally visible half-report).

Two views, never pooled (§5):

- `cutoff-reconstruction`: scores every cutoff-eligible decision that has an eligible
  final binary settlement; publication is `not_applicable`.
- `forward-shadow`: same population and information requirements, plus the decision
  must have been published timely (allowed record witnessed in `[C, C+150 s]` before T,
  no veto); every missed, vetoed, late, failed and unconfirmed opportunity stays in the
  population and in the coverage counts, unscored.

One cohort per evaluation: one dataset namespace, one research protocol hash, one
record mode and one collector configuration hash (§5 amendment). An archive holding
several collector cohorts must be evaluated once per cohort; the runner does that.

Repeat evaluation with identical manifests, boundary, view and versions returns the
existing evaluation. Different outcome or publication evidence for the same population
appends a new evaluation with `supersedes_id` and a reason; earlier evaluations are
never rewritten. Outcome evidence is read here only: nothing in this module touches
forecast selection, and the forecast path never reads settlement tables.
"""

import json
import sys
import time
import uuid
from datetime import datetime

from ..forecast import contracts, journal
from ..forecast import service as forecast_service
from ..forecast.evidence import PROOF
from ..forecast.policy import classify_publication, utc
from ..forecast.references import digest, reference
from ..provenance import RunProvenanceV1, source_hash
from ..storage import timestamp, utc_now
from . import outcomes, scoring

# evaluation-v2: one record contract per evaluation (D-001) and the
# schedule_changed_after_decision disposition (D-003).
EVALUATOR_VERSION = "evaluation-v2"
HORIZON = 3600
VIEWS = {
    "forward-shadow": "forward_shadow",
    "cutoff-reconstruction": "historical_reconstruction",
}
MAX_OPPORTUNITIES = 1000
BASELINES = ("constant-v1", "midpoint-v1")
LIMITATIONS = [
    "synthetic_evidence_is_not_trading_edge",
    "accuracy_is_not_executable_pnl: no execution model, fees, fills or validation",
    "clock_trust_is_local: receipts prove ordering on the local clock only",
    "unseen_candidate_count_unknown: the archive never proves what it did not see",
    "tiny_samples_are_descriptive_only: no significance, calibration or promotion claim",
]


def runtime_version():
    return f"python-{sys.version_info.major}.{sys.version_info.minor}-decimal-prec40-half-even"


def _ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": None if not denominator else format(scoring.Decimal(numerator) / denominator, "f"),
    }


def _count(values):
    out = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _publication_status(db, decision_id, decision, mode, namespace, view):
    """Publication disposition of a decision (§2, §6). Reconstruction, by view, never has
    one; a synthetic cohort simulating the reconstruction view follows the view."""
    if mode == "historical_reconstruction" or view == "cutoff-reconstruction":
        return "not_applicable", [], None, None
    if not decision["cutoff_eligible"]:
        return "not_attempted", ["cutoff_ineligible"], None, None
    row = db.execute(
        "SELECT id, payload_json FROM forecast_publications WHERE decision_id=?", (decision_id,)
    ).fetchone()
    if row is None:
        return "not_attempted", ["publication_record_missing"], None, None
    publication_id, payload_json = row
    publication = json.loads(payload_json)
    verdict = publication["verdict"]
    if verdict != "allowed":
        return verdict, list(publication["reasons"]), publication_id, None
    receipt = db.execute(
        "SELECT r.id, r.observed_at, json_extract(r.payload_json,'$.clock_status')"
        " FROM forecast_receipts r WHERE r.publication_id=? AND" + PROOF + " ORDER BY r.id LIMIT 1",
        (publication_id, namespace),
    ).fetchone()
    if receipt is None:
        return "publication_unconfirmed", ["receipt_missing"], publication_id, None
    receipt_id, observed_at, clock_status = receipt
    classification = classify_publication(
        scheduled_start=utc(datetime.fromisoformat(decision["scheduled_start"])),
        attempted_at=utc(datetime.fromisoformat(publication["attempted_at"])),
        published_at=utc(datetime.fromisoformat(observed_at)),
        clock_trusted=clock_status == "trusted",
        mode=mode,
    )
    reasons = [classification.reason] if classification.reason else []
    status = classification.status
    if status == "published_timely":
        return status, [], publication_id, receipt_id
    if status == "late_publication":
        return status, reasons, publication_id, receipt_id
    return status, reasons or ["unclassified"], publication_id, receipt_id


def _cohort_rows(db, protocol, dataset, modes, start_date, through_date):
    clauses = [
        "protocol=?",
        "json_extract(payload_json,'$.dataset')=?",
        "json_extract(payload_json,'$.config_hash')=?",
        "mode IN (" + ",".join("?" for _ in modes) + ")",
    ]
    params = [protocol.protocol_id, dataset, protocol.config_hash, *modes]
    if start_date is not None:
        clauses.append("substr(json_extract(payload_json,'$.scheduled_start'),1,10)>=?")
        params.append(start_date.isoformat())
    if through_date is not None:
        clauses.append("substr(json_extract(payload_json,'$.scheduled_start'),1,10)<=?")
        params.append(through_date.isoformat())
    return db.execute(
        "SELECT id, payload_json FROM forecast_decisions WHERE "
        + " AND ".join(clauses)
        + " ORDER BY cutoff, id",
        params,
    ).fetchall()


def _unattempted_bindings(
    db, protocol, binding_mode, decided_games, as_of, start_date, through_date
):
    rows = db.execute(
        "SELECT id, payload_json FROM forecast_bindings WHERE policy_version=? AND config_hash=?"
        " AND mode=? ORDER BY json_extract(payload_json,'$.cutoff'), id",
        (protocol.policy_version, protocol.config_hash, binding_mode),
    ).fetchall()
    out = []
    for binding_id, payload_json in rows:
        payload = json.loads(payload_json) | {"id": binding_id}
        if payload["game_id"] in decided_games:
            continue
        cutoff = utc(datetime.fromisoformat(payload["cutoff"]))
        day = payload["scheduled_start"][:10]
        if cutoff > as_of:
            continue  # not yet an opportunity at the outcome boundary
        if start_date is not None and day < start_date.isoformat():
            continue
        if through_date is not None and day > through_date.isoformat():
            continue
        out.append(payload)
    return out


def _unbound_games(
    db, protocol, binding_mode, namespace, known_games, as_of, start_date, through_date
):
    """Games with eligibility decisions in witnessed complete passes of this lineage but
    no binding in the cohort: counted from the frozen manifest, never silently dropped."""
    passes = db.execute(
        "SELECT p.run_id, json_extract(p.payload_json,'$.eligibility_after_id'),"
        " json_extract(p.payload_json,'$.eligibility_through_id') FROM discovery_passes p"
        " WHERE json_extract(p.payload_json,'$.complete')=1"
        " AND json_extract(p.payload_json,'$.mode')=?"
        " AND EXISTS (SELECT 1 FROM forecast_receipts r WHERE r.discovery_id=p.id AND"
        + PROOF
        + " AND json_extract(r.payload_json,'$.clock_status')='trusted')"
        " ORDER BY p.id",
        (binding_mode, namespace),
    ).fetchall()
    games = {}
    for run_id, after, through in passes:
        for game_id, reason, data_json, schedule_fetch_id in db.execute(
            "SELECT game_id, reason, data_json, schedule_fetch_id FROM eligibility"
            " WHERE run_id=? AND id>? AND id<=? AND game_id IS NOT NULL ORDER BY id",
            (run_id, after, through),
        ):
            if game_id in known_games:
                continue
            start = json.loads(data_json).get("scheduled_start")
            if start is None:
                entity = db.execute(
                    "SELECT json_extract(data_json,'$.gameDate') FROM entities"
                    " WHERE kind='mlb_game' AND ticker=? AND fetch_id=?",
                    (str(game_id), schedule_fetch_id),
                ).fetchone()
                start = entity[0] if entity else None
            entry = games.setdefault(game_id, {"reasons": {}, "scheduled_start": None})
            entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
            if start is not None:
                entry["scheduled_start"] = start.replace("Z", "+00:00")
    unresolved = set()
    for run_id, after, through in passes:
        unresolved.update(
            row[0]
            for row in db.execute(
                "SELECT DISTINCT ticker FROM eligibility WHERE run_id=? AND id>? AND id<=?"
                " AND game_id IS NULL",
                (run_id, after, through),
            )
        )
    out = []
    for game_id, entry in sorted(games.items()):
        start = entry["scheduled_start"]
        if start is None:
            continue  # no dated schedule entry: reported in diagnostics only
        start_time = utc(datetime.fromisoformat(start))
        cutoff = start_time.replace(microsecond=0) - __import__("datetime").timedelta(
            seconds=HORIZON
        )
        if cutoff > as_of:
            continue
        day = start_time.date().isoformat()
        if start_date is not None and day < start_date.isoformat():
            continue
        if through_date is not None and day > through_date.isoformat():
            continue
        out.append(
            {
                "game_id": game_id,
                "scheduled_start": start_time.isoformat(),
                "cutoff": cutoff.isoformat(),
                "reasons": dict(sorted(entry["reasons"].items())),
            }
        )
    return out, sorted(unresolved)


def evaluate(
    store,
    protocol,
    *,
    view,
    outcomes_as_of,
    dataset=None,
    source_ceiling=None,
    start_date=None,
    through_date=None,
    collector_config_hash=None,
    clock=None,
    monotonic=None,
    budget=30.0,
    record_contract=None,
):
    """Materialize one evaluation. Returns a summary dict; status complete | inconclusive
    (no paired binary outcomes) | partial (budget exhausted, nothing written) | existing.

    One evaluation covers decisions of one record contract (decision D-001): by default
    the archive's current contract (4 on schema 6, else 3); 2 evaluates a legacy cohort,
    from which draft-era decisions (not valid under the current rules, or decided before
    renewal at C existed) are excluded and listed.
    Decisions of other contracts are never unattempted opportunities; they are listed.
    """
    injected = clock is not None or monotonic is not None
    clock = clock or utc_now
    monotonic = monotonic or time.monotonic
    if view not in VIEWS:
        raise ValueError("view must be forward-shadow or cutoff-reconstruction")
    if not isinstance(budget, (int, float)) or not 0 < budget <= 600:
        raise ValueError("budget must be in (0, 600] seconds")
    if store.db.in_transaction:
        raise ValueError("evaluate requires an idle connection")
    if store.schema_version() < 5:
        raise ValueError(
            "evaluate requires record contract 3: vader forecast migrate --record-contract 3"
        )
    schema_version = store.schema_version()
    if record_contract is None:
        record_contract = 4 if schema_version >= 6 else 3
    if record_contract not in (2, 3, 4) or (record_contract == 4 and schema_version < 6):
        raise ValueError("record_contract must be 4 (schema 6), 3, or 2 (legacy cohort)")
    # Evaluation contract 2 (D-004) on schema 6: scores every registered forecaster.
    evaluation_contract = 2 if schema_version >= 6 else 1
    as_of = utc(outcomes_as_of)
    dataset = protocol.dataset if dataset is None else dataset
    namespace = protocol.observer_namespace
    db = store.db
    archive_ceiling = db.execute("SELECT COALESCE(MAX(seq),0) FROM fetches").fetchone()[0]
    if source_ceiling is None:
        source_ceiling = archive_ceiling
    if type(source_ceiling) is not int or not 0 <= source_ceiling <= archive_ceiling:
        raise ValueError("source ceiling must be an integer within the archive")
    started = monotonic()
    deadline = started + budget

    # ---- cohort and mode
    allowed_modes = (VIEWS[view], "synthetic")
    decision_rows = _cohort_rows(db, protocol, dataset, allowed_modes, start_date, through_date)
    all_decisions = [(row[0], json.loads(row[1])) for row in decision_rows]
    decisions, excluded = [], []
    for decision_id, d in all_decisions:
        contract = contracts.version("forecast_decisions", d)
        if contract != record_contract:
            excluded.append({"id": decision_id, "contract": contract, "reason": "other_contract"})
        elif contract == 2 and journal.classify(db, "forecast_decisions", d) != "current":
            excluded.append({"id": decision_id, "contract": 2, "reason": "legacy_draft"})
        elif contract == 2 and "renewal" not in d["manifest"]:
            excluded.append({"id": decision_id, "contract": 2, "reason": "pre_renewal_semantics"})
        else:
            decisions.append((decision_id, d))
    forecaster_sets = {json.dumps(d.get("forecasters", [])) for _, d in decisions}
    if len(forecaster_sets) > 1:
        raise ValueError("the cohort's decisions declare different forecaster sets")
    forecasters = json.loads(forecaster_sets.pop()) if forecaster_sets else []
    names = list(BASELINES) + [f["id"] for f in forecasters]
    modes = {d["mode"] for _, d in decisions}
    if len(modes) > 1:
        raise ValueError(f"dataset {dataset!r} mixes record modes {sorted(modes)}; not pooled")
    if modes:
        mode = modes.pop()
    else:
        # No decision yet: the cohort's bindings say whether the lineage is synthetic; an
        # injected clock (synthetic run) can only ever write a synthetic evaluation.
        binding_modes = {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT mode FROM forecast_bindings WHERE policy_version=? AND config_hash=?",
                (protocol.policy_version, protocol.config_hash),
            )
        }
        mode = "synthetic" if injected or "synthetic" in binding_modes else VIEWS[view]
    binding_mode = "synthetic" if mode == "synthetic" else "forward_shadow"
    # Collector cohorts: every pinned decision names its pass's collector hash.
    pinned = {}
    unpinned = []
    for decision_id, d in decisions:
        pin = d["manifest"].get("discovery")
        if pin is None:
            unpinned.append(decision_id)
        else:
            pinned.setdefault(pin["collector_config_hash"], []).append(decision_id)
    cohorts = sorted(pinned)
    if collector_config_hash is None:
        if len(cohorts) > 1:
            raise ValueError(
                "dataset spans collector configuration hashes "
                + ", ".join(f"{h[:12]}… ({len(pinned[h])} decisions)" for h in cohorts)
                + "; evaluate each as a separate cohort with --collector-config-hash"
            )
        collector_config_hash = cohorts[0] if cohorts else None
    elif collector_config_hash not in pinned and decisions:
        raise ValueError("no decision in this dataset pins that collector configuration hash")
    unattributed = []
    # A game decided anywhere in the dataset, under any contract, is never also an
    # unattempted opportunity.
    decided_games = {d["game_id"] for _, d in all_decisions}
    if len(cohorts) > 1:
        # With several collector cohorts an unpinned abstention belongs to none; it is
        # reported, not pooled into every cohort.
        unattributed = unpinned
        decisions = [
            (i, d)
            for i, d in decisions
            if d["manifest"].get("discovery") is not None
            and d["manifest"]["discovery"]["collector_config_hash"] == collector_config_hash
        ]
    bindings = _unattempted_bindings(
        db, protocol, binding_mode, decided_games, as_of, start_date, through_date
    )
    known_games = decided_games | {b["game_id"] for b in bindings}
    unbound, unresolved_candidates = _unbound_games(
        db, protocol, binding_mode, namespace, known_games, as_of, start_date, through_date
    )
    total = len(decisions) + len(bindings) + len(unbound)
    if total > MAX_OPPORTUNITIES:
        raise ValueError(
            f"population holds {total} opportunities, above the {MAX_OPPORTUNITIES} limit;"
            " narrow the date range"
        )

    run_id = str(uuid.uuid4())
    code_hash = source_hash()
    configuration = {
        "protocol": protocol.config_hash,
        "dataset": dataset,
        "view": view,
        "outcomes_as_of": as_of.isoformat(),
        "source_ceiling": source_ceiling,
        "start_date": None if start_date is None else start_date.isoformat(),
        "through_date": None if through_date is None else through_date.isoformat(),
        "collector_config_hash": collector_config_hash,
        "record_contract": record_contract,
    }
    store.start_run(
        RunProvenanceV1(
            run_id,
            "evaluation",
            code_hash,
            configuration,
            tool_configuration={"source_lineage": "synthetic" if injected else "live"},
            evaluation_version=EVALUATOR_VERSION,
        ),
        timestamp(clock()),
    )
    summary = {
        "run_id": run_id,
        "view": view,
        "mode": mode,
        "dataset": dataset,
        "protocol": protocol.protocol_id,
        "config_hash": protocol.config_hash,
        "collector_config_hash": collector_config_hash,
        "outcomes_as_of": as_of.isoformat(),
        "source_ceiling": source_ceiling,
        "evaluation_id": None,
        "existing": False,
        "supersedes_id": None,
        "errors": [],
    }
    try:
        population, items, annotations, outcome_refs, publication_refs = [], [], {}, {}, {}
        created_at = timestamp(clock())
        common = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": created_at,
            "code_hash": code_hash,
            "config_hash": protocol.config_hash,
            "mode": mode,
        }
        opportunities = []
        for decision_id, d in decisions:
            opportunities.append(("decision", d["cutoff"], d["game_id"], decision_id, d))
        for b in bindings:
            opportunities.append(("unattempted", b["cutoff"], b["game_id"], None, b))
        for g in unbound:
            opportunities.append(("no_binding", g["cutoff"], g["game_id"], None, g))
        opportunities.sort(key=lambda o: (o[1], o[2]))
        for origin, cutoff, game_id, decision_id, record in opportunities:
            if monotonic() >= deadline:
                raise TimeoutError("evaluation budget exhausted before the report was frozen")
            key = f"game:{game_id}"
            annotation_cutoff = cutoff
            population.append(
                {
                    "opportunity_key": key,
                    "decision_id": decision_id,
                    "game_id": game_id,
                    "horizon": HORIZON,
                }
            )
            item = common | {
                "references": [],
                "opportunity_key": key,
                "game_id": game_id,
                "horizon": HORIZON,
                "decision_id": decision_id,
                "publication_id": None,
                "receipt_id": None,
                "mapping_id": None,
                "mlb_id": None,
                "kalshi_id": None,
                "payout": None,
                "y": None,
                "scores": {name: None for name in names},
                "clipped": {name: False for name in names},
                "paired_delta": None,
            }
            if evaluation_contract == 2:
                item["contract_version"] = 2
            annotation = {"origin": origin, "cutoff": annotation_cutoff}
            if origin == "decision":
                ticker, yes_team = record["ticker"], record["yes_team_id"]
                item["cutoff_status"] = "eligible" if record["cutoff_eligible"] else "ineligible"
                item["cutoff_reasons"] = list(record["reasons"])
                status, reasons, publication_id, receipt_id = _publication_status(
                    db, decision_id, record, mode, namespace, view
                )
                item.update(
                    publication_status=status,
                    publication_reasons=reasons,
                    publication_id=publication_id,
                    receipt_id=receipt_id,
                )
                if publication_id is not None:
                    publication_refs[publication_id] = reference(
                        db, "forecast_publications", {"id": publication_id}
                    )
                if receipt_id is not None:
                    publication_refs[("receipt", receipt_id)] = reference(
                        db, "forecast_receipts", {"id": receipt_id}
                    )
                probabilities = {name: record["results"][name]["probability"] for name in names}
                annotation["midpoint_reasons"] = record["results"]["midpoint-v1"]["reasons"]
            elif origin == "unattempted":
                ticker, yes_team = record["ticker"], record["yes_team_id"]
                facts = forecast_service.cutoff_facts(
                    db,
                    binding=record,
                    namespace=namespace,
                    evidence_mode=binding_mode,
                    selection_version=protocol.selection_version,
                )
                item["cutoff_status"] = "unattempted"
                item["cutoff_reasons"] = ["decision_missing"]
                item.update(
                    publication_status="not_applicable"
                    if view == "cutoff-reconstruction"
                    else "not_attempted",
                    publication_reasons=["decision_missing"],
                )
                # Explicitly labelled cutoff-policy reconstruction annotation; it inserts
                # no decision and supplies no forward probability (§5).
                annotation["cutoff_policy_annotation"] = {
                    "binding_id": record["id"],
                    "cutoff_eligible": facts.cutoff_eligible,
                    "reasons": list(facts.reasons),
                    "midpoint_available": facts.midpoint_probability is not None,
                    "midpoint_reasons": list(facts.midpoint_reasons),
                }
                probabilities = {name: None for name in names}
            else:
                ticker, yes_team = None, None
                item["cutoff_status"] = "no_binding"
                item["cutoff_reasons"] = ["binding_missing"]
                item.update(
                    publication_status="not_applicable"
                    if view == "cutoff-reconstruction"
                    else "not_attempted",
                    publication_reasons=["binding_missing"],
                )
                annotation["eligibility_reasons"] = record["reasons"]
                probabilities = {name: None for name in names}
            outcome = (
                outcomes.join(
                    db,
                    ticker=ticker,
                    game_id=game_id,
                    yes_team_id=yes_team,
                    outcomes_as_of=as_of,
                    source_ceiling=source_ceiling,
                    mode=mode,
                    scheduled_start=None
                    if record.get("scheduled_start") is None
                    else utc(datetime.fromisoformat(record["scheduled_start"])),
                )
                if ticker is not None
                else outcomes.Outcome(
                    "identity_unresolved",
                    ("binding_missing" if origin == "no_binding" else "no_selected_contract",),
                )
            )
            item.update(
                outcome_status=outcome.status,
                outcome_reasons=list(outcome.reasons),
                kalshi_id=outcome.kalshi_id,
                mapping_id=outcome.mapping_id,
                mlb_id=outcome.mlb_id,
                references=list(outcome.references),
            )
            payout = outcome.payout
            if payout is not None:
                try:
                    contracts.probability(payout)
                except ValueError:
                    item["outcome_reasons"].append("payout_unrepresentable:" + payout[:32])
                    payout = None
            item["payout"] = payout
            annotation.update(
                provider_status=outcome.provider_status,
                flags=list(outcome.flags),
                outcome_fetch_id=outcome.fetch_id,
                outcome_retrieved_at=outcome.retrieved_at,
                mlb_winner_team_id=outcome.mlb_winner_team_id,
                mlb_agrees=outcome.mlb_agrees,
            )
            for ref in outcome.references:
                outcome_refs[ref["key"]["id"]] = ref
            scorable = outcome.scorable and (
                (view == "forward-shadow" and item["publication_status"] == "published_timely")
                or (view == "cutoff-reconstruction" and item["cutoff_status"] == "eligible")
            )
            if scorable:
                item["y"] = outcome.y
                for name in names:
                    if probabilities[name] is not None:
                        item["scores"][name], item["clipped"][name] = scoring.score_pair(
                            probabilities[name], outcome.y
                        )
                if all(item["scores"][name] is not None for name in BASELINES):
                    with scoring.arithmetic():
                        item["paired_delta"] = {
                            metric: format(
                                scoring.Decimal(item["scores"]["midpoint-v1"][metric])
                                - scoring.Decimal(item["scores"]["constant-v1"][metric]),
                                "f",
                            )
                            for metric in ("brier", "log_loss")
                        }
            annotation["probabilities"] = probabilities
            annotations[key] = annotation
            items.append(item)

        report = _report(
            view,
            mode,
            dataset,
            protocol,
            collector_config_hash,
            code_hash,
            as_of,
            source_ceiling,
            start_date,
            through_date,
            items,
            annotations,
            unattributed,
            cohorts,
            pinned,
            unresolved_candidates,
        )
        report["record_contract"] = record_contract
        report["forecasters"] = forecaster_report(items, forecasters)
        report["excluded_decisions"] = excluded[:1000]
        publication_manifest = [
            publication_refs[k]
            for k in sorted(publication_refs, key=lambda k: (str(type(k)), str(k)))
        ]
        outcome_manifest = [outcome_refs[k] for k in sorted(outcome_refs)]
        status = "complete" if report["scores"]["paired"]["n"] > 0 else "inconclusive"
        payload = common | {
            "references": [],
            "dataset": dataset,
            "protocol": protocol.protocol_id,
            "view": view,
            "evaluator_version": EVALUATOR_VERSION,
            "runtime_version": runtime_version(),
            "outcomes_as_of": as_of.isoformat(),
            "source_ceiling": source_ceiling,
            "population_manifest": population,
            "publication_manifest": publication_manifest,
            "outcome_manifest": outcome_manifest,
            "publication_digest": digest(publication_manifest),
            "outcome_digest": digest(outcome_manifest),
            "status": status,
            "report": report,
            "supersedes_id": None,
            "correction_reason": None,
        }
        if evaluation_contract == 2:
            payload["contract_version"] = 2
            payload["forecasters"] = forecasters
        existing = db.execute(
            "SELECT id, json_extract(payload_json,'$.status') FROM evaluation_runs"
            " WHERE idempotency_key=?",
            (contracts.key("evaluation_runs", payload),),
        ).fetchone()
        if existing is not None:
            summary.update(evaluation_id=existing[0], existing=True, status=existing[1])
            summary["report"] = report
            store.finish_run(run_id, "complete", summary)
            return summary
        previous = db.execute(
            "SELECT id, json_extract(payload_json,'$.publication_digest'),"
            " json_extract(payload_json,'$.outcome_digest'), json_extract(payload_json,'$.code_hash'),"
            " json_extract(payload_json,'$.evaluator_version'),"
            " json_extract(payload_json,'$.runtime_version')"
            " FROM evaluation_runs WHERE json_extract(payload_json,'$.dataset')=? AND mode=?"
            " AND json_extract(payload_json,'$.protocol')=?"
            " AND json_extract(payload_json,'$.config_hash')=? AND view=?"
            " AND json_extract(payload_json,'$.population_manifest')=?"
            " ORDER BY id DESC LIMIT 1",
            (
                dataset,
                mode,
                protocol.protocol_id,
                protocol.config_hash,
                view,
                __import__("vader_intelligence.provenance", fromlist=["json_text"]).json_text(
                    population
                ),
            ),
        ).fetchone()
        if previous is not None:
            changed = []
            if previous[1] != payload["publication_digest"]:
                changed.append("publication_evidence_changed")
            if previous[2] != payload["outcome_digest"]:
                changed.append("outcome_evidence_changed")
            if previous[3] != code_hash:
                changed.append("code_changed")
            if (previous[4], previous[5]) != (EVALUATOR_VERSION, payload["runtime_version"]):
                changed.append("evaluator_changed")
            if not changed:
                changed.append("boundary_changed")  # same manifests, other as-of/ceiling
            payload["supersedes_id"] = previous[0]
            payload["correction_reason"] = "+".join(changed)
            report["supersedes"] = {"id": previous[0], "reason": payload["correction_reason"]}
        payload["report"]["items_digest"] = digest(items)
        evaluation_id = journal.append_evaluation(db, payload, items)
        summary.update(
            evaluation_id=evaluation_id,
            status=status,
            supersedes_id=payload["supersedes_id"],
            report=report,
        )
    except TimeoutError as exc:
        # Nothing was written: the report transaction rolled back or never started. The
        # frozen manifest digest is retained for a deterministic retry.
        summary["errors"].append(str(exc)[:300])
        summary["status"] = "partial"
        summary["population_digest"] = digest(population)
        store.finish_run(run_id, "partial", summary)
        return summary
    except BaseException as exc:
        summary["errors"].append(str(exc)[:300])
        store.finish_run(run_id, "failed", summary | {"status": "failed"})
        raise
    summary["runtime_seconds"] = monotonic() - started
    store.finish_run(run_id, summary["status"], summary)
    return summary


def _report(
    view,
    mode,
    dataset,
    protocol,
    collector_config_hash,
    code_hash,
    as_of,
    source_ceiling,
    start_date,
    through_date,
    items,
    annotations,
    unattributed,
    cohorts,
    pinned,
    unresolved_candidates,
):
    decisions = [i for i in items if i["decision_id"] is not None]
    eligible = [i for i in decisions if i["cutoff_status"] == "eligible"]
    midpoint_valid = [
        i for i in eligible if annotations[i["opportunity_key"]]["probabilities"]["midpoint-v1"]
    ]
    unattempted = [i for i in items if i["cutoff_status"] == "unattempted"]
    annotated_eligible = [
        i
        for i in unattempted
        if annotations[i["opportunity_key"]]["cutoff_policy_annotation"]["cutoff_eligible"]
    ]
    annotated_midpoint = [
        i
        for i in annotated_eligible
        if annotations[i["opportunity_key"]]["cutoff_policy_annotation"]["midpoint_available"]
    ]
    timely = [i for i in eligible if i["publication_status"] == "published_timely"]
    timely_midpoint = [i for i in timely if i in midpoint_valid]
    scored_constant = [i for i in items if i["scores"]["constant-v1"] is not None]
    scored_paired = [i for i in items if i["paired_delta"] is not None]
    U, E = len(items), len(eligible)
    K, M, P = E, len(midpoint_valid), len(midpoint_valid)
    FK, FM, FP = len(timely), len(timely_midpoint), len(timely_midpoint)
    paired = scoring.paired_summary(
        [
            {"constant-v1": i["scores"]["constant-v1"], "midpoint-v1": i["scores"]["midpoint-v1"]}
            for i in items
        ]
    )
    constant_only = {
        "n": len(scored_constant),
        "mean_brier": scoring.mean(i["scores"]["constant-v1"]["brier"] for i in scored_constant),
        "mean_log_loss": scoring.mean(
            i["scores"]["constant-v1"]["log_loss"] for i in scored_constant
        ),
    }
    clipped = sum(1 for i in items for name in BASELINES if i["clipped"][name])
    primary_cutoff = _count(
        (i["cutoff_reasons"][0] if i["cutoff_reasons"] else "eligible") for i in items
    )
    cutoffs = sorted(annotations[i["opportunity_key"]].get("cutoff") or "" for i in items)
    report = {
        "evaluator_version": EVALUATOR_VERSION,
        "runtime_version": runtime_version(),
        "view": view,
        "evidence_mode": mode,
        "simulated_view": mode == "synthetic",
        "dataset": dataset,
        "protocol": protocol.protocol_id,
        "research_config_hash": protocol.config_hash,
        "collector_config_hash": collector_config_hash,
        "code_hash": code_hash,
        "policy": {
            "baselines": list(BASELINES),
            "policy_version": protocol.policy_version,
            "selection_version": protocol.selection_version,
            "publication_policy": protocol.publication_policy,
            "log_epsilon": protocol.log_epsilon,
        },
        "period": {
            "start_date": None if start_date is None else start_date.isoformat(),
            "through_date": None if through_date is None else through_date.isoformat(),
            "outcomes_as_of": as_of.isoformat(),
            "source_ceiling": source_ceiling,
            "earliest_cutoff": min((i_cut for i_cut in cutoffs if i_cut), default=None),
            "latest_cutoff": max((i_cut for i_cut in cutoffs if i_cut), default=None),
        },
        "population": {
            "U": U,
            "E": E,
            "K": K,
            "M": M,
            "P": P,
            "FK": FK,
            "FM": FM,
            "FP": FP,
            "decisions": len(decisions),
            "abstentions": len(decisions) - E,
            "unattempted": len(unattempted),
            "no_binding": sum(1 for i in items if i["cutoff_status"] == "no_binding"),
            # Candidate contracts that resolved no official game: an unresolved event
            # group, never guessed into a game opportunity (specification §5).
            "unresolved_candidates": len(unresolved_candidates),
            "annotated_eligible_unattempted": len(annotated_eligible),
            "annotated_midpoint_unattempted": len(annotated_midpoint),
            "scored_constant": len(scored_constant),
            "scored_paired": len(scored_paired),
        },
        "coverage": {
            "cutoff": {
                "K/U": _ratio(K, U),
                "M/U": _ratio(M, U),
                "P/U": _ratio(P, U),
                "K/E": _ratio(K, E),
                "M/E": _ratio(M, E),
            },
            "publication": {
                "FK/K": _ratio(FK, K),
                "FM/M": _ratio(FM, M),
                "FP/P": _ratio(FP, P),
                "FK/U": _ratio(FK, U),
                "FM/U": _ratio(FM, U),
                "FP/U": _ratio(FP, U),
            }
            if view == "forward-shadow"
            else None,
            "binary_outcome": {
                "paired_scored/P": _ratio(len(scored_paired), P),
                "paired_scored/FP": _ratio(len(scored_paired), FP)
                if view == "forward-shadow"
                else None,
            },
        },
        "cutoff_status": _count(i["cutoff_status"] for i in items),
        "cutoff_primary_reason": primary_cutoff,
        "publication_status": _count(i["publication_status"] for i in items),
        "publication_reasons": _count(r for i in items for r in i["publication_reasons"]),
        "outcome_status": _count(i["outcome_status"] for i in items),
        "outcome_reasons": _count(r for i in items for r in i["outcome_reasons"]),
        "mlb_annotation": _count(
            str(annotations[i["opportunity_key"]].get("mlb_agrees")) for i in items
        ),
        "scores": {"paired": paired, "constant_only": constant_only, "clipped_count": clipped},
        "collector_cohorts_in_dataset": {h: len(ids) for h, ids in sorted(pinned.items())},
        "unattributed_decisions": unattributed,
        "unresolved_candidates": unresolved_candidates,
        "annotations": annotations,
        "provenance_limitations": list(LIMITATIONS),
        "cost": {"external_calls": "0", "local_compute": "unmeasured"},
    }
    return report


def forecaster_report(items, forecasters):
    """Per registered forecaster: scored N and means, and the paired comparison with the
    midpoint baseline on the items where both were scored (D-004). Same population and
    dispositions as the baselines; nothing is averaged across forecasters."""
    out = {}
    for forecaster in forecasters:
        name = forecaster["id"]
        scored = [i for i in items if i["scores"].get(name) is not None]
        common = [i for i in scored if i["scores"]["midpoint-v1"] is not None]
        with scoring.arithmetic():
            deltas = {
                metric: scoring.mean(
                    format(
                        scoring.Decimal(i["scores"][name][metric])
                        - scoring.Decimal(i["scores"]["midpoint-v1"][metric]),
                        "f",
                    )
                    for i in common
                )
                if common
                else None
                for metric in ("brier", "log_loss")
            }
        out[name] = {
            "artifact_sha256": forecaster["artifact_sha256"],
            "family": forecaster["family"],
            "feature_set": forecaster["feature_set"],
            "n": len(scored),
            "mean_brier": scoring.mean(i["scores"][name]["brier"] for i in scored),
            "mean_log_loss": scoring.mean(i["scores"][name]["log_loss"] for i in scored),
            "paired_with_midpoint": {
                "n": len(common),
                "mean_brier_delta": deltas["brier"],
                "mean_log_loss_delta": deltas["log_loss"],
            },
            "clipped": sum(1 for i in items if i["clipped"].get(name)),
        }
    return out


def inspect_evaluation(db, evaluation_id, *, items=False):
    row = db.execute(
        "SELECT id, digest, payload_json FROM evaluation_runs WHERE id=?", (evaluation_id,)
    ).fetchone()
    if row is None:
        raise ValueError("missing evaluation")
    payload = json.loads(row[2])
    result = {"evaluation_id": row[0], "digest": row[1], "evaluation": payload}
    if items:
        result["items"] = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload_json FROM evaluation_items WHERE evaluation_id=?"
                " ORDER BY opportunity_key",
                (evaluation_id,),
            )
        ]
    result["superseded_by"] = [
        r[0]
        for r in db.execute(
            "SELECT id FROM evaluation_runs WHERE supersedes_id=? ORDER BY id", (evaluation_id,)
        )
    ]
    return result


def list_evaluations(db, *, limit=20, before=None, dataset=None, view=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer in [1, 100]")
    clauses, params = [], []
    if before:
        clauses.append("id<?")
        params.append(before)
    if dataset:
        clauses.append("json_extract(payload_json,'$.dataset')=?")
        params.append(dataset)
    if view:
        clauses.append("view=?")
        params.append(view)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.execute(
        f"SELECT id, payload_json FROM evaluation_runs {where} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    out = []
    for evaluation_id, payload_json in rows:
        p = json.loads(payload_json)
        out.append(
            {
                "evaluation_id": evaluation_id,
                "view": p["view"],
                "mode": p["mode"],
                "dataset": p["dataset"],
                "status": p["status"],
                "created_at": p["created_at"],
                "outcomes_as_of": p["outcomes_as_of"],
                "supersedes_id": p["supersedes_id"],
                "population": p["report"]["population"],
                "paired": p["report"]["scores"]["paired"],
            }
        )
    return {"count": len(out), "evaluations": out}
