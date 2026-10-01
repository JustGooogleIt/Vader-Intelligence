"""Version-1 storage envelopes. No discovery, source selection, generation or joins."""

import re
from datetime import datetime

from ..evaluation.scoring import probability
from ..provenance import json_text
from .references import digest

COMMON = set("schema_version run_id created_at code_hash config_hash mode references".split())
FIELDS = {
    "discovery_passes": "session_id phase_sequence completed_at complete reasons counts eligibility_after_id eligibility_through_id manifest",
    "forecast_bindings": "candidate_key revision previous_id policy_version game_id event_ticker ticker yes_team_id team_ids original_date original_start game_number scheduled_start cutoff status reasons manifest terms_digest",
    "forecast_receipts": "subject_kind fetch_id binding_id publication_id discovery_id subject_digest observed_at session_id observer_namespace monotonic_offset clock_status",
    "forecast_decisions": "dataset protocol game_id unresolved_key ticker yes_team_id horizon scheduled_start cutoff binding_id discovery_id selection_version baseline_versions cutoff_eligible reasons results manifest source_ceiling supersedes_id",
    "forecast_publications": "decision_id decision_digest attempted_at verdict publication_policy reasons manifest",
    "evaluation_runs": "dataset protocol view evaluator_version runtime_version outcomes_as_of source_ceiling population_manifest publication_manifest outcome_manifest publication_digest outcome_digest status report supersedes_id correction_reason",
    "evaluation_items": "evaluation_id opportunity_key game_id horizon decision_id publication_id receipt_id mapping_id mlb_id kalshi_id cutoff_status cutoff_reasons publication_status publication_reasons outcome_status outcome_reasons payout y scores clipped paired_delta",
}
FIELDS = {k: COMMON | set(v.split()) for k, v in FIELDS.items()}
MODES = {"synthetic", "forward_shadow", "historical_reconstruction"}


def identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
        raise ValueError("expected bounded nonempty identifier")


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("expected SHA-256")


def date(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("expected aware UTC timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("expected aware UTC timestamp")
    return parsed


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("expected bounded integer")
    if value > 9223372036854775807:
        raise ValueError("integer overflow")


def reasons(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("expected bounded reason list")
    for reason in value:
        identifier(reason)


def validate(table, p):
    if table not in FIELDS or not isinstance(p, dict) or set(p) != FIELDS[table]:
        raise ValueError("incomplete or unknown storage fields")
    if type(p["schema_version"]) is not int or p["schema_version"] != 1 or p["mode"] not in MODES:
        raise ValueError("unsupported record schema/mode")
    encoded = json_text(p)
    if len(encoded.encode()) > 10 * 1024 * 1024:
        raise ValueError("record exceeds 10 MiB")
    identifier(p["run_id"])
    date(p["created_at"])
    sha(p["code_hash"])
    sha(p["config_hash"])
    if not isinstance(p["references"], list) or len(p["references"]) > 10000:
        raise ValueError("invalid evidence references")
    for ref in p["references"]:
        if not isinstance(ref, dict) or "table" not in ref:
            raise ValueError("evidence references must be typed")
    for key in ("reasons", "cutoff_reasons", "publication_reasons", "outcome_reasons"):
        if key in p:
            reasons(p[key])
    for key in (
        "game_id",
        "yes_team_id",
        "binding_id",
        "discovery_id",
        "previous_id",
        "supersedes_id",
        "decision_id",
        "publication_id",
        "receipt_id",
        "evaluation_id",
        "mapping_id",
        "mlb_id",
        "kalshi_id",
        "game_number",
    ):
        if p.get(key) is not None:
            integer(p[key], 1)
    for key in (
        "cutoff",
        "scheduled_start",
        "original_start",
        "observed_at",
        "attempted_at",
        "completed_at",
        "outcomes_as_of",
    ):
        if p.get(key) is not None:
            date(p[key])
    for key in (
        "dataset",
        "protocol",
        "policy_version",
        "selection_version",
        "publication_policy",
        "evaluator_version",
        "runtime_version",
        "session_id",
        "observer_namespace",
        "candidate_key",
        "opportunity_key",
        "status",
        "cutoff_status",
        "outcome_status",
    ):
        if key in p:
            identifier(p[key])
    for key in ("manifest", "report"):
        if key in p and not isinstance(p[key], dict):
            raise ValueError("expected structured manifest/report")
    if "source_ceiling" in p:
        integer(p["source_ceiling"])
    if "horizon" in p and (type(p["horizon"]) is not int or p["horizon"] != 3600):
        raise ValueError("v1 horizon must be 3600")
    if table == "discovery_passes":
        integer(p["phase_sequence"], 1)
        for k in ("eligibility_after_id", "eligibility_through_id"):
            integer(p[k])
        if (
            type(p["complete"]) is not bool
            or p["eligibility_after_id"] > p["eligibility_through_id"]
        ):
            raise ValueError("invalid discovery bounds/completeness")
        if not isinstance(p["counts"], dict) or set(p["counts"]) != {"games", "contracts"}:
            raise ValueError("incomplete discovery counts")
        for value in p["counts"].values():
            integer(value)
        if not p["complete"] and not p["reasons"]:
            raise ValueError("incomplete discovery requires reasons")
        if date(p["completed_at"]) > date(p["created_at"]):
            raise ValueError("discovery completion after insertion")
    elif table == "forecast_bindings":
        integer(p["revision"], 1)
        if (p["revision"] == 1) != (p["previous_id"] is None):
            raise ValueError("binding chain required")
        if not isinstance(p["team_ids"], list) or len(p["team_ids"]) > 2:
            raise ValueError("invalid team IDs")
        for team in p["team_ids"]:
            integer(team, 1)
        if p["terms_digest"] is not None:
            sha(p["terms_digest"])
    elif table == "forecast_receipts":
        sha(p["subject_digest"])
        if p["clock_status"] not in ("trusted", "untrusted"):
            raise ValueError("invalid clock status")
        if not isinstance(p["monotonic_offset"], str) or not re.fullmatch(
            r"[0-9]{1,12}(\.[0-9]{1,9})?", p["monotonic_offset"]
        ):
            raise ValueError("invalid monotonic offset")
        if date(p["observed_at"]) > date(p["created_at"]):
            raise ValueError("observation after receipt insertion")
    elif table == "forecast_decisions":
        if type(p["cutoff_eligible"]) is not bool or bool(p["reasons"]) == p["cutoff_eligible"]:
            raise ValueError("inconsistent cutoff reasons")
        if p["game_id"] is None:
            identifier(p["unresolved_key"])
        elif p["unresolved_key"] is not None:
            raise ValueError("resolved decision cannot have unresolved key")
        expected = {"constant-v1", "midpoint-v1"}
        if (
            p["baseline_versions"] != ["constant-v1", "midpoint-v1"]
            or not isinstance(p["results"], dict)
            or set(p["results"]) != expected
        ):
            raise ValueError("fixed baseline pair required")
        for name, value in p["results"].items():
            if (
                not isinstance(value, dict)
                or set(value) != {"probability", "reasons", "inputs"}
                or not isinstance(value["inputs"], dict)
            ):
                raise ValueError("incomplete baseline result")
            reasons(value["reasons"])
            if value["probability"] is None:
                if not value["reasons"]:
                    raise ValueError("abstention requires reasons")
            else:
                if (
                    not isinstance(value["probability"], str)
                    or value["reasons"]
                    or not p["cutoff_eligible"]
                ):
                    raise ValueError("inconsistent baseline probability")
                prob = probability(value["probability"])
                if name == "constant-v1" and prob != probability("0.5"):
                    raise ValueError("constant must be 0.5")
        if p["cutoff_eligible"] and (
            p["results"]["constant-v1"]["probability"] is None
            or any(
                p[k] is None
                for k in (
                    "game_id",
                    "ticker",
                    "yes_team_id",
                    "binding_id",
                    "discovery_id",
                    "scheduled_start",
                    "cutoff",
                )
            )
        ):
            raise ValueError("eligible decision missing identity/evidence")
        if p["supersedes_id"] is not None and p["mode"] == "forward_shadow":
            raise ValueError("correction cannot be a forward replacement")
    elif table == "forecast_publications":
        sha(p["decision_digest"])
        if p["mode"] == "historical_reconstruction" or p["verdict"] not in (
            "allowed",
            "vetoed",
            "missed",
            "failed",
        ):
            raise ValueError("invalid publication mode/verdict")
        if (p["verdict"] == "allowed") == bool(p["reasons"]):
            raise ValueError("publication verdict/reasons mismatch")
        if date(p["attempted_at"]) > date(p["created_at"]):
            raise ValueError("publication attempt after insertion")
    elif table == "evaluation_runs":
        date(p["outcomes_as_of"])
        if p["view"] not in ("cutoff-reconstruction", "forward-shadow") or (
            p["mode"] != "synthetic"
            and p["mode"]
            != {
                "cutoff-reconstruction": "historical_reconstruction",
                "forward-shadow": "forward_shadow",
            }[p["view"]]
        ):
            raise ValueError("incompatible evaluation view/mode")
        for key in ("population_manifest", "publication_manifest", "outcome_manifest"):
            if not isinstance(p[key], list) or len(p[key]) > 1000:
                raise ValueError("evaluation manifest limit")
        for key in ("publication", "outcome"):
            if p[key + "_digest"] != digest(p[key + "_manifest"]):
                raise ValueError("evaluation manifest digest mismatch")
        if p["status"] not in ("complete", "inconclusive"):
            raise ValueError("no partial evaluation reports")
        if (p["supersedes_id"] is None) != (p["correction_reason"] is None):
            raise ValueError("correction reason required")
    elif table == "evaluation_items":
        if p["publication_status"] not in (
            "not_attempted",
            "not_applicable",
            "published_timely",
            "vetoed",
            "missed",
            "failed",
            "late_publication",
            "publication_unconfirmed",
        ):
            raise ValueError("invalid publication status")
        if p["y"] is not None and (type(p["y"]) is not int or p["y"] not in (0, 1)):
            raise ValueError("binary label must be distinct from payout")
        if p["payout"] is not None:
            probability(p["payout"])
        if (
            not isinstance(p["scores"], dict)
            or set(p["scores"]) != {"constant-v1", "midpoint-v1"}
            or not isinstance(p["clipped"], dict)
            or set(p["clipped"]) != set(p["scores"])
        ):
            raise ValueError("fixed score pair required")
        for name, score in p["scores"].items():
            if type(p["clipped"][name]) is not bool:
                raise ValueError("explicit clipping flag required")
            if score is not None:
                if (
                    p["y"] is None
                    or p["kalshi_id"] is None
                    or p["decision_id"] is None
                    or not isinstance(score, dict)
                    or set(score) != {"brier", "log_loss"}
                ):
                    raise ValueError("score missing binary evidence")
                for value in score.values():
                    decimal_text(value)
        if p["paired_delta"] is not None:
            if (
                any(v is None for v in p["scores"].values())
                or not isinstance(p["paired_delta"], dict)
                or set(p["paired_delta"]) != {"brier", "log_loss"}
            ):
                raise ValueError("paired deltas require both scores")
            for value in p["paired_delta"].values():
                decimal_text(value, signed=True)
    return encoded


def decimal_text(value, signed=False):
    pattern = r"-?[0-9]+(\.[0-9]+)?" if signed else r"[0-9]+(\.[0-9]+)?"
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(pattern, value):
        raise ValueError("exact finite score string required")


def key(table, p):
    if table == "discovery_passes":
        values = [p["run_id"], p["session_id"]]
    elif table == "forecast_bindings":
        values = [
            p[k] for k in ("candidate_key", "policy_version", "config_hash", "mode", "revision")
        ]
    elif table == "forecast_receipts":
        values = [
            p[k]
            for k in (
                "subject_kind",
                "fetch_id",
                "binding_id",
                "publication_id",
                "discovery_id",
                "subject_digest",
                "observer_namespace",
                "mode",
            )
        ]
    elif table == "forecast_decisions":
        values = [
            p[k]
            for k in (
                "dataset",
                "mode",
                "protocol",
                "config_hash",
                "game_id",
                "unresolved_key",
                "horizon",
            )
        ]
    elif table == "forecast_publications":
        values = [p["decision_id"]]
    elif table == "evaluation_runs":
        values = [
            p[k]
            for k in (
                "dataset",
                "mode",
                "protocol",
                "config_hash",
                "code_hash",
                "view",
                "evaluator_version",
                "runtime_version",
                "outcomes_as_of",
                "source_ceiling",
                "population_manifest",
                "publication_manifest",
                "outcome_manifest",
            )
        ]
    else:
        values = [p["evaluation_id"], p["opportunity_key"]]
    return digest([table, *values])
