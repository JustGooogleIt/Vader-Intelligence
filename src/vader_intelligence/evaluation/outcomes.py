"""Join one frozen opportunity to the archived outcome view (specification §5).

Read-only. The outcome view is bounded by an explicit `outcomes_as_of` retrieval-time
boundary and a fetch-sequence `source_ceiling`: only settlement attempts retrieved at
or before that time and within that ceiling exist for the join. For the selected
contract the **latest completed settlement-market attempt** by ingestion sequence is
used, never the latest finalized result, so a later pending, disputed or malformed
response invalidates binary eligibility rather than resurrecting an older label.

Binary labels come solely from `evaluation.settlement.finalized_binary` on the
selected contract's own Kalshi record; the MLB result is an independent annotation
and never selects or replaces a label. The settlement mapping, when present, must
confirm the frozen game identity for a binary label to score.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from ..forecast.references import reference
from ..settlement.models import kalshi_result
from .scoring import arithmetic
from .settlement import finalized_binary

DISPOSITIONS = (
    "binary_scorable",
    "pending",
    "disputed",
    "exceptional_payout",
    "invalid_or_missing_outcome",
    "identity_unresolved",
    "identity_changed",
    "schedule_changed_after_decision",
    "outcome_fetch_failed",
    "outcome_not_observed",
)
COMPLETED = ("ok", "parse_error", "error")
EXCEPTIONAL = {"not_binary_payout", "result_payout_conflict"}


@dataclass(frozen=True)
class Outcome:
    status: str
    reasons: tuple
    kalshi_id: int | None = None
    mapping_id: int | None = None
    mlb_id: int | None = None
    payout: str | None = None
    y: int | None = None
    fetch_id: str | None = None
    retrieved_at: str | None = None
    provider_status: str | None = None
    flags: tuple = ()
    mlb_winner_team_id: int | None = None
    mlb_agrees: bool | None = None
    references: list = field(default_factory=list)

    @property
    def scorable(self):
        return self.status == "binary_scorable" and self.y is not None


def _version_within(db, kind, entity_key, outcomes_as_of, source_ceiling):
    """Latest revision whose first retrieval is inside the frozen outcome view."""
    return db.execute(
        "SELECT v.id, v.revision, v.data_json FROM settlement_versions v"
        " JOIN fetches f ON f.id=v.first_fetch_id WHERE v.kind=? AND v.entity_key=?"
        " AND f.seq<=? AND f.retrieved_at IS NOT NULL AND f.retrieved_at<=?"
        " ORDER BY v.revision DESC LIMIT 1",
        (kind, str(entity_key), source_ceiling, outcomes_as_of.isoformat()),
    ).fetchone()


def join(db, *, ticker, game_id, yes_team_id, outcomes_as_of, source_ceiling, scheduled_start=None):
    """Outcome disposition for one opportunity. `outcomes_as_of` is an aware datetime.

    `scheduled_start` is the start the opportunity was decided for. When the settlement
    mapping confirms the same game but at another start (a postponement or time change
    after the decision, decision D-003), the opportunity is no longer a horizon-3600
    forecast of that game: the disposition is `schedule_changed_after_decision`,
    explicit and never scored. A mapping without a start cannot confirm the horizon.
    """
    if ticker is None:
        return Outcome("identity_unresolved", ("no_selected_contract",))
    attempt = db.execute(
        "SELECT f.id, f.seq, f.state, f.retrieved_at FROM fetches f"
        " JOIN requests r ON r.id=f.request_id"
        " JOIN settlement_targets t ON t.id=r.request_key AND t.run_id=r.run_id"
        " WHERE r.stage='settlement-market' AND t.ticker=? AND f.retrieved_at IS NOT NULL"
        " AND f.retrieved_at<=? AND f.seq<=? AND f.state IN ('ok','parse_error','error')"
        " ORDER BY f.seq DESC LIMIT 1",
        (ticker, outcomes_as_of.isoformat(), source_ceiling),
    ).fetchone()
    if attempt is None:
        return Outcome("outcome_not_observed", ("no_settlement_attempt_in_view",))
    fetch_id, _seq, state, retrieved_at = attempt
    base = {"fetch_id": fetch_id, "retrieved_at": retrieved_at}
    if state != "ok":
        return Outcome("outcome_fetch_failed", (f"latest_attempt_{state}",), **base)
    observed = db.execute(
        "SELECT version_id FROM settlement_observations WHERE kind='kalshi' AND entity_key=?"
        " AND fetch_id=? ORDER BY parser_version DESC LIMIT 1",
        (ticker, fetch_id),
    ).fetchone()
    if observed is None:
        return Outcome("invalid_or_missing_outcome", ("no_kalshi_observation",), **base)
    kalshi_id = observed[0]
    data = json.loads(
        db.execute("SELECT data_json FROM settlement_versions WHERE id=?", (kalshi_id,)).fetchone()[
            0
        ]
    )
    references = [reference(db, "settlement_versions", {"id": kalshi_id})]
    with arithmetic():
        interpretation = kalshi_result(data) if "lifecycle" not in data else data
    eligibility = finalized_binary(data)
    provider_status = data.get("status")
    flags = tuple(interpretation.get("flags", []))
    payout = (
        data.get("settlement_value_dollars")
        if isinstance(data.get("settlement_value_dollars"), str)
        else None
    )
    base.update(
        kalshi_id=kalshi_id,
        provider_status=provider_status,
        flags=flags,
        payout=payout,
        references=references,
    )
    mapping = _version_within(db, "mapping", ticker, outcomes_as_of, source_ceiling)
    mapping_id = mapping_data = None
    if mapping is not None:
        mapping_id, _, mapping_json = mapping
        mapping_data = json.loads(mapping_json)
        base["mapping_id"] = mapping_id
        base["references"].append(reference(db, "settlement_versions", {"id": mapping_id}))
    mlb = _version_within(db, "mlb", game_id, outcomes_as_of, source_ceiling) if game_id else None
    if mlb is not None:
        mlb_id, _, mlb_json = mlb
        base["mlb_id"] = mlb_id
        base["mlb_winner_team_id"] = json.loads(mlb_json).get("winner_team_id")
        base["references"].append(reference(db, "settlement_versions", {"id": mlb_id}))
    if not eligibility.eligible:
        reasons = tuple(eligibility.reasons)
        if "not_finalized" in reasons:
            status = "disputed" if provider_status == "disputed" else "pending"
        elif EXCEPTIONAL & set(reasons):
            status = "exceptional_payout"
        else:
            status = "invalid_or_missing_outcome"
        return Outcome(status, reasons, **base)
    # A binary label scores only against the frozen game identity.
    if mapping_data is None:
        return Outcome("identity_unresolved", ("mapping_missing",), **base)
    if mapping_data.get("status") != "mapped":
        return Outcome(
            "identity_unresolved",
            ("mapping_" + str(mapping_data.get("status")), str(mapping_data.get("reason"))),
            **base,
        )
    if mapping_data.get("game_id") != game_id:
        return Outcome("identity_changed", ("mapping_game_mismatch",), **base)
    if scheduled_start is not None:
        played = mapping_data.get("scheduled_start")
        if not isinstance(played, str):
            return Outcome("schedule_changed_after_decision", ("mapping_start_unknown",), **base)
        if datetime.fromisoformat(played.replace("Z", "+00:00")) != scheduled_start:
            return Outcome(
                "schedule_changed_after_decision",
                ("start_moved:" + str(mapping_data.get("reason")),),
                **base,
            )
    y = eligibility.label
    winner = base.get("mlb_winner_team_id")
    agrees = None
    if winner is not None and yes_team_id is not None:
        agrees = (winner == yes_team_id) == (y == 1)
    return Outcome("binary_scorable", (), y=y, mlb_agrees=agrees, **base)
