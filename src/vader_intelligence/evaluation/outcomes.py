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
confirm the frozen game identity for a binary label to score. It is the mapping of the
selected attempt's own run; only when that run's mapping is incomplete (its schedule
fetch failed or lies beyond the view) does the most recent earlier run with complete
mapping evidence stand in, and only if it was mapped from a schedule showing the game
final with the same finalized Kalshi result (review F50).

Attempts follow the evaluation's lineage: a forward-shadow or reconstruction evaluation
sees only attempts of settlement runs whose durable source lineage is live, so synthetic
bytes in the same archive can neither label nor shadow its outcomes; a synthetic
evaluation may use either lineage.
"""

import json
from dataclasses import dataclass, field

from ..forecast.references import reference, run_source_mode
from ..settlement.models import instant, kalshi_result
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
MODES = ("synthetic", "forward_shadow", "historical_reconstruction")
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


def _target_mapping(db, mapping_id, anchor_fetch_id, outcomes_as_of, source_ceiling):
    """(id, revision, data_json) of the mapping a settlement run recorded for its target,
    when that run's mapping evidence lies inside the frozen outcome view; else None."""
    if mapping_id is None or anchor_fetch_id is None:
        return None
    inside = db.execute(
        "SELECT 1 FROM fetches WHERE id=? AND seq<=? AND retrieved_at IS NOT NULL"
        " AND retrieved_at<=?",
        (anchor_fetch_id, source_ceiling, outcomes_as_of.isoformat()),
    ).fetchone()
    if inside is None:
        return None
    return db.execute(
        "SELECT id, revision, data_json FROM settlement_versions WHERE id=? AND kind='mapping'",
        (mapping_id,),
    ).fetchone()


def _final_mapping(db, earlier, *, ticker, label, mode, outcomes_as_of, source_ceiling):
    """The mapping of the most recent earlier settlement run whose mapping evidence is
    complete inside the view, when it cannot carry a stale start (review F50): mapped from
    a schedule showing that game final (a mapping made after the game was played), in a
    run whose own Kalshi record is the same finalized binary result. Any other complete
    mapping ends the search, so older evidence never skips newer contrary evidence. Runs
    without a mapping in view (a failed fetch, evidence beyond the ceiling) are passed
    over; runs of another lineage do not exist for a live evaluation (F10)."""
    for fetch_id, _seq, state, _at, mapping_id, anchor, run_id in earlier:
        if state != "ok" or (mode != "synthetic" and run_source_mode(db, run_id) != "live"):
            continue
        mapping = _target_mapping(db, mapping_id, anchor, outcomes_as_of, source_ceiling)
        if mapping is None:
            continue
        data = json.loads(mapping[2])
        if data.get("status") != "mapped":
            return None
        rows = db.execute(
            "SELECT o.kind, v.data_json FROM settlement_observations o"
            " JOIN settlement_versions v ON v.id=o.version_id"
            " WHERE (o.kind='mlb' AND o.entity_key=? AND o.fetch_id=?)"
            " OR (o.kind='kalshi' AND o.entity_key=? AND o.fetch_id=?)"
            " ORDER BY o.parser_version",
            (str(data.get("game_id")), anchor, ticker, fetch_id),
        ).fetchall()
        seen = {kind: json.loads(value) for kind, value in rows}  # highest parser wins
        final = (seen.get("mlb") or {}).get("lifecycle") == "final"
        if not final or finalized_binary(seen.get("kalshi")).label != label:
            return None
        return mapping
    return None


def join(
    db, *, ticker, game_id, yes_team_id, outcomes_as_of, source_ceiling, mode, scheduled_start=None
):
    """Outcome disposition for one opportunity. `outcomes_as_of` is an aware datetime.

    `mode` is the evaluation's evidence mode: other than `synthetic`, only attempts of
    live-lineage settlement runs exist for the join.

    `scheduled_start` is the start the opportunity was decided for. When the settlement
    mapping confirms the same game but at another start (a postponement or time change
    after the decision, decision D-003), the opportunity is no longer a horizon-3600
    forecast of that game: the disposition is `schedule_changed_after_decision`,
    explicit and never scored. A mapping without a start it states as an aware instant
    (missing, unparseable or offset-less) cannot confirm the horizon.
    """
    if mode not in MODES:
        raise ValueError("unknown evaluation mode")
    if ticker is None:
        return Outcome("identity_unresolved", ("no_selected_contract",))
    attempts = db.execute(
        "SELECT f.id, f.seq, f.state, f.retrieved_at, t.mapping_version_id,"
        " COALESCE(t.schedule_fetch_id, t.event_fetch_id, t.market_fetch_id), r.run_id"
        " FROM fetches f JOIN requests r ON r.id=f.request_id"
        " JOIN settlement_targets t ON t.id=r.request_key AND t.run_id=r.run_id"
        " WHERE r.stage='settlement-market' AND t.ticker=? AND f.retrieved_at IS NOT NULL"
        " AND f.retrieved_at<=? AND f.seq<=? AND f.state IN ('ok','parse_error','error')"
        " ORDER BY f.seq DESC",
        (ticker, outcomes_as_of.isoformat(), source_ceiling),
    ).fetchall()
    # Synthetic bytes are never evidence for a live evaluation (M1): their attempts are
    # skipped, not "the latest attempt" that labels or invalidates the outcome.
    attempt = next(
        (a for a in attempts if mode == "synthetic" or run_source_mode(db, a[6]) == "live"), None
    )
    if attempt is None:
        return Outcome("outcome_not_observed", ("no_settlement_attempt_in_view",))
    fetch_id, _seq, state, retrieved_at, target_mapping_id, mapping_anchor, _run = attempt
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
    # The mapping must come from the same settlement run as the Kalshi result, with its
    # evidence (the run's schedule retrieval) inside the view: an older mapping could
    # still show a start the game no longer has (a postponement whose schedule fetch
    # failed or lies beyond the source ceiling).
    mapping = _target_mapping(db, target_mapping_id, mapping_anchor, outcomes_as_of, source_ceiling)
    if mapping is None and eligibility.eligible:
        # Only exception (F50): when that run's mapping is incomplete, a mapping made
        # after the game was played (schedule final, same finalized result) cannot be
        # stale, so a provider outage does not silently drop a proven game.
        mapping = _final_mapping(
            db,
            attempts[attempts.index(attempt) + 1 :],
            ticker=ticker,
            label=eligibility.label,
            mode=mode,
            outcomes_as_of=outcomes_as_of,
            source_ceiling=source_ceiling,
        )
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
        return Outcome("identity_unresolved", ("mapping_incomplete",), **base)
    if mapping_data.get("status") != "mapped":
        return Outcome(
            "identity_unresolved",
            ("mapping_" + str(mapping_data.get("status")), str(mapping_data.get("reason"))),
            **base,
        )
    if mapping_data.get("game_id") != game_id:
        return Outcome("identity_changed", ("mapping_game_mismatch",), **base)
    if scheduled_start is not None:
        try:
            played = instant(mapping_data.get("scheduled_start"))
        except ValueError:
            # Policy 2 keeps the provider's gameDate verbatim: a missing, unparseable or
            # offset-less start is reported, never raised and never taken as a move.
            return Outcome("schedule_changed_after_decision", ("mapping_start_unknown",), **base)
        if played != scheduled_start:
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
