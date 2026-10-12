"""Build the immutable discovery-pass summary from committed rows.

No network, no clock. The collector records what Phase A did in a DiscoveryOutcome;
build_summary turns that plus the committed fetch and eligibility rows into the
version-1 discovery_passes envelope that forecast.journal validates and stores.
Completeness never depends on book requests, book results or run status.
"""

import json

from ..normalize import PARSER_VERSION
from ..provenance import json_text
from .references import digest, reference

MANIFEST_VERSION = 1
MANIFEST_LIMIT = 1024 * 1024
COLLECTION_PROTOCOL = "two-phase-v1"
PHASES = (
    "none",
    "status",
    "series",
    "documents",
    "pages",
    "schedule",
    "events",
    "eligibility",
    "done",
)
# Fixed reason order (plan §3.1 table, then bounds, reconciliation and size).
REASON_ORDER = (
    "status_unavailable",
    "series_unavailable",
    "series_identity_mismatch",
    "document_unavailable",
    "pages_incomplete",
    "page_limit_exceeded",
    "market_limit_exceeded",
    "cursor_repeated",
    "schedule_unavailable",
    "event_unavailable",
    "event_identity_mismatch",
    "event_membership_ambiguous",
    "eligibility_incomplete",
    "deadline_exhausted",
    "access_denied",
    "interrupted",
    "internal_error",
    "manifest_too_large",
)
STAGE_REASONS = {
    "status": "status_unavailable",
    "series": "series_unavailable",
    "documents": "document_unavailable",
    "pages": "pages_incomplete",
    "schedule": "schedule_unavailable",
    "events": "event_unavailable",
}
# Reasons that end Phase A before the event loop can finish. Candidates left
# undecided by them are explained by them; only an event that answered without
# covering a discovered contract yields eligibility_incomplete.
PHASE_ENDING = frozenset(REASON_ORDER) - {
    "event_unavailable",
    "event_identity_mismatch",
    "event_membership_ambiguous",
    "eligibility_incomplete",
    "internal_error",
    "manifest_too_large",
}
COMPLETED_STATES = ("ok", "parse_error", "error")


class DiscoveryOutcome:
    """Mutable Phase A record kept by the collector; consumed once by build_summary."""

    def __init__(self, *, kind, resumed):
        self.kind = kind
        self.resumed = resumed
        self.started_at = None
        self.phase_reached = "none"
        self.reasons = set()
        self.failures = []
        self.used_fetch_ids = set()
        self.page_trace = []
        self.documents = []
        self.request = None
        self.schedule_fetch_ids = []
        self.official_game_ids = set()
        self.candidates = {}
        self.candidates_known = False
        self.events = []
        self.eligibility_started = False

    def reached(self, phase):
        if PHASES.index(phase) > PHASES.index(self.phase_reached):
            self.phase_reached = phase

    def fail(self, reason, stage, key, error):
        if reason not in REASON_ORDER:
            raise ValueError("unknown discovery reason")
        self.reasons.add(reason)
        if len(self.failures) < 100:
            self.failures.append({"stage": stage, "key": str(key)[:256], "error": str(error)[:300]})

    def use(self, response):
        self.used_fetch_ids.add(response.fetch_id)
        return response


def ordered_reasons(reasons):
    return [r for r in REASON_ORDER if r in reasons]


def run_config_hash(db, run_id):
    """Digest of the writing run's recorded collector configuration (plan §9.2)."""
    row = db.execute("SELECT provenance_json FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("missing run")
    provenance = json.loads(row[0])
    return digest(provenance["configuration"]), provenance["code_hash"]


def build_summary(
    db,
    *,
    run_id,
    session_id,
    completed_at,
    created_at,
    eligibility_after_id,
    fetch_seq_after,
    config,
    mode,
    outcome,
):
    """Return the discovery_passes envelope for one invocation. Reads committed rows only."""
    config_hash, code_hash = run_config_hash(db, run_id)
    phase_sequence = (
        db.execute("SELECT COUNT(*) FROM discovery_passes WHERE run_id=?", (run_id,)).fetchone()[0]
        + 1
    )
    reasons = set(outcome.reasons)

    # Fetch evidence of this invocation's discovery phase: every attempt of this run
    # after the invocation started. Books have not been requested yet.
    rows = db.execute(
        "SELECT f.id, f.seq, f.state, f.retrieved_at FROM fetches f "
        "JOIN requests r ON r.id=f.request_id WHERE r.run_id=? AND f.seq>? ORDER BY f.seq",
        (run_id, fetch_seq_after),
    ).fetchall()
    fetch_seq_through = max([fetch_seq_after] + [row[1] for row in rows])
    completed = set()
    incomplete_attempts = []
    for fetch_id, seq, state, retrieved_at in rows:
        if state in COMPLETED_STATES and retrieved_at:
            completed.add(fetch_id)
        else:
            incomplete_attempts.append({"fetch_id": fetch_id, "seq": seq, "state": state})
    # Checkpointed responses reused from an earlier session of the same run.
    for fetch_id in outcome.used_fetch_ids:
        row = db.execute(
            "SELECT state, retrieved_at FROM fetches WHERE id=?", (fetch_id,)
        ).fetchone()
        if row is None:
            raise ValueError("used fetch missing from archive")
        if row[0] in COMPLETED_STATES and row[1]:
            completed.add(fetch_id)
    references = [reference(db, "fetches", {"id": fetch_id}) for fetch_id in sorted(completed)]

    # Eligibility decisions recorded inside this phase's bounds, this run only.
    eligibility_through_id = db.execute("SELECT COALESCE(MAX(id),0) FROM eligibility").fetchone()[0]
    cursor = db.execute(
        "SELECT * FROM eligibility WHERE run_id=? AND id>? AND id<=? ORDER BY id",
        (run_id, eligibility_after_id, eligibility_through_id),
    )
    names = [d[0] for d in cursor.description]
    decisions = []
    for values in cursor:
        row = dict(zip(names, values))
        decisions.append(
            {
                "id": row["id"],
                "ticker": row["ticker"],
                "eligible": bool(row["eligible"]),
                "reason": row["reason"],
                "game_id": row["game_id"],
                "cutoff": row["cutoff"],
                "schedule_fetch_id": row["schedule_fetch_id"],
                "market_fetch_id": row["market_fetch_id"],
                "row_digest": digest(row),
            }
        )
    decided = {d["ticker"] for d in decisions}
    eligible_contracts = {d["ticker"] for d in decisions if d["eligible"]}
    eligible_games = {d["game_id"] for d in decisions if d["eligible"] and d["game_id"] is not None}
    reason_counts = {}
    for d in decisions:
        reason_counts[d["reason"]] = reason_counts.get(d["reason"], 0) + 1

    candidates = [
        {"ticker": ticker, "event_ticker": event_ticker, "page_fetch_id": page_fetch_id}
        for ticker, (event_ticker, page_fetch_id) in sorted(outcome.candidates.items())
    ]
    candidate_events = sorted({c["event_ticker"] for c in candidates})
    eligibility_complete = outcome.candidates_known and set(outcome.candidates) <= decided
    answered = {e["event_ticker"] for e in outcome.events if e["fetch_id"] and e["error"] is None}
    unexplained = {
        ticker
        for ticker, (event_ticker, _) in outcome.candidates.items()
        if ticker not in decided and event_ticker in answered
    }
    if outcome.candidates_known and not eligibility_complete:
        if (unexplained and not reasons & PHASE_ENDING) or not reasons:
            reasons.add("eligibility_incomplete")
    pages_complete = outcome.candidates_known and not reasons & {
        "pages_incomplete",
        "page_limit_exceeded",
        "market_limit_exceeded",
        "cursor_repeated",
    }

    # Reconciliation (E7 and E9): decisions must cite this phase's schedule and event
    # evidence, candidate decisions must be for discovered candidates, and counts
    # must describe the rows they summarize.
    event_fetch_ids = {e["fetch_id"] for e in outcome.events if e["fetch_id"]}
    schedule_ids = set(outcome.schedule_fetch_ids)
    for d in decisions:
        if (
            d["ticker"] not in outcome.candidates
            or d["schedule_fetch_id"] not in schedule_ids
            or d["market_fetch_id"] not in event_fetch_ids
        ):
            reasons.add("internal_error")
            if len(outcome.failures) < 100:
                outcome.failures.append(
                    {
                        "stage": "eligibility",
                        "key": d["ticker"],
                        "error": "decision cites unknown evidence",
                    }
                )
    if outcome.phase_reached == "done" and not eligibility_complete:
        reasons.add("internal_error")
    complete = (
        not reasons
        and outcome.phase_reached == "done"
        and outcome.candidates_known
        and eligibility_complete
        and pages_complete
    )
    if complete and (outcome.failures or incomplete_attempts):
        # A complete pass has no failure and no attempt left without a completed retrieval.
        reasons.add("internal_error")
        complete = False

    matched_games = {d["game_id"] for d in decisions if d["game_id"] is not None}
    decided_events = {
        outcome.candidates[d["ticker"]][0]
        for d in decisions
        if d["ticker"] in outcome.candidates and d["game_id"] is not None
    }

    def page_entry(entry):
        attempts = db.execute(
            "SELECT COUNT(*) FROM fetches WHERE request_id="
            "(SELECT request_id FROM fetches WHERE id=?)",
            (entry["fetch_id"],),
        ).fetchone()[0]
        return {
            "request_key": entry["request_key"],
            "fetch_id": entry["fetch_id"],
            "attempts": attempts,
            "has_next": entry["has_next"],
        }

    counting = [p for p in outcome.page_trace if p["counts"]]
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "kind": outcome.kind,
        "resumed": outcome.resumed,
        "started_at": outcome.started_at,
        "fetch_seq_after": fetch_seq_after,
        "fetch_seq_through": fetch_seq_through,
        "phase_reached": outcome.phase_reached,
        "candidates_known": outcome.candidates_known,
        "eligibility_started": outcome.eligibility_started,
        "limits": {
            "max_pages": config.max_pages,
            "page_size": config.page_size,
            "max_markets": config.max_markets,
            "schedule_max_age": config.schedule_max_age,
            "pregame_buffer": config.pregame_buffer,
            "lookahead_days": config.lookahead_days,
        },
        "request": outcome.request,
        "policy": {
            "parser_version": PARSER_VERSION,
            "mapping_version": 1,
            "predicate": "scope.eligible",
            "code_hash": code_hash,
            "collection_protocol": COLLECTION_PROTOCOL,
        },
        "pages": {
            "chain": [page_entry(p) for p in counting],
            "discarded": [page_entry(p) for p in outcome.page_trace if not p["counts"]],
            "restarted": any(not p["counts"] for p in outcome.page_trace),
            "terminal": bool(counting) and not counting[-1]["has_next"],
            "page_count": len(counting),
        },
        "documents": sorted(outcome.documents, key=lambda d: d["url"]),
        "schedule_fetch_ids": list(outcome.schedule_fetch_ids),
        "official_game_ids": sorted(outcome.official_game_ids),
        "candidates": candidates,
        "events": sorted(outcome.events, key=lambda e: e["event_ticker"]),
        "eligibility": decisions,
        "unmatched_events": sorted(set(candidate_events) - decided_events),
        "unmatched_games": sorted(outcome.official_game_ids - matched_games),
        "candidate_counts": {"contracts": len(candidates), "events": len(candidate_events)},
        "reason_counts": dict(sorted(reason_counts.items())),
        "pages_complete": pages_complete,
        "eligibility_complete": eligibility_complete,
        "incomplete_attempts": incomplete_attempts,
        "failures": outcome.failures[:100],
    }
    if len(json_text(manifest).encode()) > MANIFEST_LIMIT:
        reasons.add("manifest_too_large")
        complete = False
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "kind": outcome.kind,
            "resumed": outcome.resumed,
            "started_at": outcome.started_at,
            "fetch_seq_after": fetch_seq_after,
            "fetch_seq_through": fetch_seq_through,
            "phase_reached": outcome.phase_reached,
            "candidates_known": outcome.candidates_known,
            "eligibility_started": outcome.eligibility_started,
            "candidate_counts": {"contracts": len(candidates), "events": len(candidate_events)},
            "reason_counts": dict(sorted(reason_counts.items())),
            "pages_complete": pages_complete,
            "eligibility_complete": eligibility_complete,
        }
        references = []
    return {
        "schema_version": 1,
        "run_id": run_id,
        "session_id": session_id,
        "phase_sequence": phase_sequence,
        "created_at": created_at,
        "completed_at": completed_at,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "mode": mode,
        "complete": complete,
        "reasons": ordered_reasons(reasons),
        "counts": {"contracts": len(eligible_contracts), "games": len(eligible_games)},
        "eligibility_after_id": eligibility_after_id,
        "eligibility_through_id": eligibility_through_id,
        "references": references,
        "manifest": manifest,
    }
