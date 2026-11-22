"""Machine-readable and concise human-readable evaluation reports (milestone 4).

Reports are generated from committed evaluation rows after the fact; they are never a
second source of truth. Display rounds to six decimal places; stored scores stay exact.
A comparison of two evaluations restricts the paired metrics to the opportunities both
scored and shows the coverage difference instead of presenting unequal samples as
directly comparable. Evaluations of different views, modes or protocols are never
compared, and different collector cohorts are labelled, not pooled.
"""

import json
from decimal import ROUND_HALF_EVEN, Decimal

from ..evaluation.scoring import paired_summary
from ..forecast.references import digest

SIX = Decimal("0.000001")


def rounded(value):
    if value is None:
        return "null"
    return format(Decimal(value).quantize(SIX, rounding=ROUND_HALF_EVEN), "f")


def _short(value):
    return "null" if value is None else str(value)[:12]


def _ratio(entry):
    if entry is None:
        return "n/a"
    return f"{entry['numerator']}/{entry['denominator']}"


def load(db, evaluation_id):
    row = db.execute(
        "SELECT id, digest, payload_json FROM evaluation_runs WHERE id=?", (evaluation_id,)
    ).fetchone()
    if row is None:
        raise ValueError("missing evaluation")
    payload = json.loads(row[2])
    items = {
        item["opportunity_key"]: item
        for item in (
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload_json FROM evaluation_items WHERE evaluation_id=?"
                " ORDER BY opportunity_key",
                (evaluation_id,),
            )
        )
    }
    return {"evaluation_id": row[0], "digest": row[1], "evaluation": payload, "items": items}


def machine_report(db, evaluation_id, *, include_items=False):
    """The stored report plus identities, as one JSON-serializable document."""
    loaded = load(db, evaluation_id)
    evaluation = loaded["evaluation"]
    document = {
        "evaluation_id": loaded["evaluation_id"],
        "evaluation_digest": loaded["digest"],
        "view": evaluation["view"],
        "mode": evaluation["mode"],
        "status": evaluation["status"],
        "created_at": evaluation["created_at"],
        "supersedes_id": evaluation["supersedes_id"],
        "correction_reason": evaluation["correction_reason"],
        "outcomes_as_of": evaluation["outcomes_as_of"],
        "source_ceiling": evaluation["source_ceiling"],
        "population_manifest_digest": digest(evaluation["population_manifest"]),
        "publication_digest": evaluation["publication_digest"],
        "outcome_digest": evaluation["outcome_digest"],
        "report": evaluation["report"],
    }
    if include_items:
        document["items"] = list(loaded["items"].values())
    return document


def text_report(db, evaluation_id):
    loaded = load(db, evaluation_id)
    e = loaded["evaluation"]
    r = e["report"]
    pop, cov, scores = r["population"], r["coverage"], r["scores"]
    simulated = " (simulated view: synthetic evidence)" if r.get("simulated_view") else ""
    lines = [
        f"Evaluation #{loaded['evaluation_id']}  view={e['view']}  mode={e['mode']}{simulated}"
        f"  status={e['status']}",
        f"Dataset {e['dataset']}  protocol {e['protocol']}  research-config {_short(r['research_config_hash'])}"
        f"  collector-config {_short(r['collector_config_hash'])}  code {_short(r['code_hash'])}"
        f"  evaluator {r['evaluator_version']}/{r['runtime_version']}",
        f"Period: dates {r['period']['start_date'] or 'all'}..{r['period']['through_date'] or 'all'}"
        f"  cutoffs {r['period']['earliest_cutoff'] or 'none'}..{r['period']['latest_cutoff'] or 'none'}"
        f"  outcomes as of {r['period']['outcomes_as_of']}  ceiling {r['period']['source_ceiling']}",
        f"Population: U={pop['U']} E={pop['E']} K={pop['K']} M={pop['M']} P={pop['P']}"
        + (
            f" | FK={pop['FK']} FM={pop['FM']} FP={pop['FP']}"
            if e["view"] == "forward-shadow"
            else ""
        )
        + f" | decisions {pop['decisions']} abstentions {pop['abstentions']}"
        f" unattempted {pop['unattempted']} no-binding {pop['no_binding']}"
        f" unresolved-candidates {pop['unresolved_candidates']}",
        f"Cutoff coverage: K/U {_ratio(cov['cutoff']['K/U'])}  M/U {_ratio(cov['cutoff']['M/U'])}"
        f"  P/U {_ratio(cov['cutoff']['P/U'])}  K/E {_ratio(cov['cutoff']['K/E'])}  M/E {_ratio(cov['cutoff']['M/E'])}",
    ]
    if cov.get("publication"):
        p = cov["publication"]
        lines.append(
            f"Publication coverage: FK/K {_ratio(p['FK/K'])}  FM/M {_ratio(p['FM/M'])}  FP/P {_ratio(p['FP/P'])}"
            f"  FK/U {_ratio(p['FK/U'])}  FM/U {_ratio(p['FM/U'])}  FP/U {_ratio(p['FP/U'])}"
        )
    lines.append(
        f"Binary outcome coverage: paired scored/P {_ratio(cov['binary_outcome']['paired_scored/P'])}"
        + (
            f"  paired scored/FP {_ratio(cov['binary_outcome']['paired_scored/FP'])}"
            if cov["binary_outcome"].get("paired_scored/FP")
            else ""
        )
    )
    lines.append("Cutoff primary reasons: " + _counts(r["cutoff_primary_reason"]))
    lines.append("Publication status: " + _counts(r["publication_status"]))
    if r["publication_reasons"]:
        lines.append("Publication reasons: " + _counts(r["publication_reasons"]))
    lines.append("Outcome status: " + _counts(r["outcome_status"]))
    if r["outcome_reasons"]:
        lines.append("Outcome reasons: " + _counts(r["outcome_reasons"]))
    paired = scores["paired"]
    lines.append(
        f"Paired scores (n={paired['n']}): Brier constant {rounded(paired['mean_brier_constant'])}"
        f" midpoint {rounded(paired['mean_brier_midpoint'])} delta {rounded(paired['mean_brier_delta'])}"
        f" | log loss constant {rounded(paired['mean_log_loss_constant'])}"
        f" midpoint {rounded(paired['mean_log_loss_midpoint'])} delta {rounded(paired['mean_log_loss_delta'])}"
        f" | clipped {scores['clipped_count']}"
    )
    only = scores["constant_only"]
    lines.append(
        f"Constant-only (n={only['n']}): Brier {rounded(only['mean_brier'])}"
        f" log loss {rounded(only['mean_log_loss'])}"
    )
    if r.get("supersedes"):
        lines.append(
            f"Supersedes evaluation #{r['supersedes']['id']} ({r['supersedes']['reason']})"
        )
    if r.get("unattributed_decisions"):
        lines.append(
            f"Unattributed decisions (other cohorts present): {r['unattributed_decisions']}"
        )
    contract = r.get("record_contract")
    lines.append(
        "Record contract: "
        + (str(contract) if contract is not None else "unrecorded (evaluated before D-001)")
    )
    for name, f in sorted((r.get("forecasters") or {}).items()):
        paired = f["paired_with_midpoint"]
        lines.append(
            f"Forecaster {name} ({f['family']}, {f['feature_set']}) n={f['n']}: Brier "
            f"{rounded(f['mean_brier'])} log loss {rounded(f['mean_log_loss'])} | vs midpoint "
            f"(n={paired['n']}) Brier delta {rounded(paired['mean_brier_delta'])} log loss "
            f"delta {rounded(paired['mean_log_loss_delta'])} | clipped {f['clipped']}"
        )
    if r.get("excluded_decisions"):
        excluded = {}
        for entry in r["excluded_decisions"]:
            label = f"contract {entry['contract']} {entry['reason']}"
            excluded[label] = excluded.get(label, 0) + 1
        lines.append("Decisions outside this evaluation: " + _counts(excluded))
    lines.append(
        "Not a trading result: "
        + "; ".join(limit.split(":")[0].replace("_", " ") for limit in r["provenance_limitations"])
    )
    return "\n".join(lines)


def _counts(mapping):
    return ", ".join(f"{k} {v}" for k, v in mapping.items()) or "none"


def compare(db, first_id, second_id):
    """Compare two evaluations on the opportunities both scored (paired), exposing the
    coverage difference. Refuses different views, modes or protocols."""
    a, b = load(db, first_id), load(db, second_id)
    ea, eb = a["evaluation"], b["evaluation"]
    for field in ("view", "mode", "protocol"):
        if ea[field] != eb[field]:
            raise ValueError(f"evaluations differ in {field}; never pooled or compared")
    if ea["config_hash"] != eb["config_hash"]:
        raise ValueError("evaluations use different research protocol configurations")
    if ea["report"].get("record_contract") != eb["report"].get("record_contract"):
        raise ValueError("evaluations cover different record contracts; never pooled or compared")
    if sorted(ea["report"].get("forecasters") or {}) != sorted(
        eb["report"].get("forecasters") or {}
    ):
        raise ValueError("evaluations score different forecaster sets; never pooled or compared")
    keys_a, keys_b = set(a["items"]), set(b["items"])
    scored_a = {k for k, i in a["items"].items() if i["paired_delta"] is not None}
    scored_b = {k for k, i in b["items"].items() if i["paired_delta"] is not None}
    common = sorted(scored_a & scored_b)
    constant_a = {k for k, i in a["items"].items() if i["scores"]["constant-v1"] is not None}
    constant_b = {k for k, i in b["items"].items() if i["scores"]["constant-v1"] is not None}

    def restricted(items, keys):
        return paired_summary([items[k]["scores"] for k in keys])

    def summarize(loaded):
        e = loaded["evaluation"]
        return {
            "evaluation_id": loaded["evaluation_id"],
            "dataset": e["dataset"],
            "collector_config_hash": e["report"]["collector_config_hash"],
            "status": e["status"],
            "outcomes_as_of": e["outcomes_as_of"],
            "population": e["report"]["population"],
            "paired_all": e["report"]["scores"]["paired"],
        }

    same_cohort = ea["report"]["collector_config_hash"] == eb["report"]["collector_config_hash"]
    same_population = ea["population_manifest"] == eb["population_manifest"]
    return {
        "view": ea["view"],
        "mode": ea["mode"],
        "protocol": ea["protocol"],
        "same_population_manifest": same_population,
        "same_collector_cohort": same_cohort,
        "cohort_note": None
        if same_cohort
        else "different collector configuration cohorts: shown side by side, never pooled",
        "first": summarize(a),
        "second": summarize(b),
        "coverage_difference": {
            "opportunities_only_in_first": sorted(keys_a - keys_b),
            "opportunities_only_in_second": sorted(keys_b - keys_a),
            "paired_scored_only_in_first": sorted(scored_a - scored_b),
            "paired_scored_only_in_second": sorted(scored_b - scored_a),
            "constant_scored_only_in_first": sorted(constant_a - constant_b),
            "constant_scored_only_in_second": sorted(constant_b - constant_a),
        },
        "common_paired": {
            "n": len(common),
            "opportunities": common,
            "first": restricted(a["items"], common),
            "second": restricted(b["items"], common),
        },
        "per_opportunity": {
            key: {
                "first": a["items"][key]["paired_delta"],
                "second": b["items"][key]["paired_delta"],
                "y_first": a["items"][key]["y"],
                "y_second": b["items"][key]["y"],
            }
            for key in common
        },
    }


def text_compare(comparison):
    c = comparison
    lines = [
        f"Compare evaluations #{c['first']['evaluation_id']} and #{c['second']['evaluation_id']}"
        f"  view={c['view']} mode={c['mode']} protocol={c['protocol']}",
        f"Same population manifest: {c['same_population_manifest']}  same collector cohort: {c['same_collector_cohort']}",
    ]
    if c["cohort_note"]:
        lines.append("Note: " + c["cohort_note"])
    for name in ("first", "second"):
        s = c[name]
        pop = s["population"]
        lines.append(
            f"{name.capitalize()}: #{s['evaluation_id']} dataset {s['dataset']} cohort"
            f" {_short(s['collector_config_hash'])} status {s['status']} U={pop['U']} P={pop['P']}"
            f" paired-scored={pop['scored_paired']} outcomes as of {s['outcomes_as_of']}"
        )
    d = c["coverage_difference"]
    lines.append(
        f"Coverage difference: only-in-first {len(d['opportunities_only_in_first'])}"
        f" only-in-second {len(d['opportunities_only_in_second'])}"
        f" paired-only-in-first {len(d['paired_scored_only_in_first'])}"
        f" paired-only-in-second {len(d['paired_scored_only_in_second'])}"
    )
    cp = c["common_paired"]
    lines.append(
        f"Common paired opportunities n={cp['n']}: Brier delta first {rounded(cp['first']['mean_brier_delta'])}"
        f" second {rounded(cp['second']['mean_brier_delta'])} | log loss delta first"
        f" {rounded(cp['first']['mean_log_loss_delta'])} second {rounded(cp['second']['mean_log_loss_delta'])}"
    )
    lines.append("Not a trading result; coverage differences are shown, not averaged away.")
    return "\n".join(lines)


def export(path, document):
    """Write a machine-readable document to a new file (never overwrite)."""
    from pathlib import Path

    target = Path(path)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite {target}")
    target.write_text(json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return str(target)
