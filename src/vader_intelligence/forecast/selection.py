"""Choose within one already identity-qualified game's candidates, without hindsight.

select_contract fixes the ticker from the identity binding before any price is read.
select_inputs then chooses the midpoint book input at the cutoff, with complete
rejection evidence and no backward search for an older attractive book (specification
§4), under the protocol's selection version (decision D-016):

- `selection-v1` anchors on the latest completed book retrieval received by C and
  abstains when that retrieval was not also witnessed by C. That departs from §4: under
  the specified cycle order (tick, then collect) the latest retrieval before C is
  normally witnessed only after C, so the midpoint almost always abstains. It is kept
  unchanged so decisions written under it recompute identically.
- `selection-v2` is §4 as written: the latest completed retrieval received **and**
  witnessed by C.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..normalize import identifier
from .policy import utc

SELECTION_V1 = "selection-v1"
SELECTION_V2 = "selection-v2"
SELECTION_VERSIONS = (SELECTION_V1, SELECTION_V2)
# selection-v2 looks for an attempt witnessed by C among this many of the latest
# completed attempts received by C; past the bound it reports the latest attempt's
# availability reason, which is what it reports when none qualifies.
V2_SCAN_LIMIT = 200


@dataclass(frozen=True, order=True)
class Contract:
    event_ticker: str
    ticker: str
    yes_team_id: int


@dataclass(frozen=True)
class Selection:
    contract: Contract | None
    reasons: tuple[str, ...]
    alternatives: tuple[Contract, ...]


def select_contract(candidates, team_ids):
    """Read only event_ticker/ticker/yes_team_id; ignore price/status/outcome fields.

    Caller supplies all qualified candidates for one game, including closed or
    bookless listings. Never prefilter for forecast success. At most 200 contracts.
    Open status, notional and current-rule validation happen AFTER this selection.
    """
    if (
        not isinstance(team_ids, (tuple, list))
        or len(team_ids) != 2
        or any(type(t) is not int or t <= 0 for t in team_ids)
        or len(set(team_ids)) != 2
    ):
        raise ValueError("exactly two distinct positive integer team IDs required")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("candidates must be a bounded sequence")
    if len(candidates) > 200:
        raise ValueError("contract limit exceeded; selection incomplete")
    parsed = set()
    for item in candidates:
        if not isinstance(item, Mapping):
            raise ValueError("candidate must be a mapping")
        event, ticker = identifier(item.get("event_ticker")), identifier(item.get("ticker"))
        team = item.get("yes_team_id")
        if not event.startswith("KXMLBGAME-") or not ticker.startswith("KXMLBGAME-"):
            raise ValueError("candidate outside allowed family")
        if type(team) is not int or team <= 0:
            raise ValueError("candidate must have a positive integer team ID")
        parsed.add(Contract(event, ticker, team))
    ordered = tuple(sorted(parsed))
    if not ordered:
        return Selection(None, ("no_contract_known",), ())
    event = ordered[0].event_ticker
    chosen = [c for c in ordered if c.event_ticker == event]
    if len({c.ticker for c in chosen}) != len(chosen):
        return Selection(None, ("ambiguous_contract",), ordered)
    lower = min(team_ids)
    if lower not in {c.yes_team_id for c in chosen}:
        return Selection(None, ("selected_team_missing",), ordered)
    if {c.yes_team_id for c in chosen} != set(team_ids):
        return Selection(None, ("event_team_set_mismatch",), ordered)
    selected = min((c for c in chosen if c.yes_team_id == lower), key=lambda c: c.ticker)
    return Selection(selected, (), tuple(c for c in ordered if c != selected))


@dataclass(frozen=True)
class BookInput:
    fetch_id: str
    retrieved_at: datetime
    observed_at: datetime
    receipt_id: int
    raw: dict


@dataclass(frozen=True)
class InputsOrAbstention:
    book: BookInput | None
    reasons: tuple[str, ...]

    @property
    def eligible(self):
        return self.book is not None and not self.reasons


def select_inputs(
    db,
    *,
    ticker,
    game_id,
    cutoff,
    namespace,
    mode,
    scheduled_start=None,
    version=SELECTION_V1,
):
    """Return the chosen book input for the midpoint baseline, or book-stage reasons.

    Reads only; writes nothing. Completed book attempts for the ticker received at or
    before C are ordered by retrieval time then seq. Under `selection-v1` the anchor is
    the latest of them, and it must also be witnessed by C. Under `selection-v2` the
    anchor is the latest of them whose first receipt is trusted and observed at or before
    C; when none is, the latest attempt's availability reason is reported, as in v1.
    Either way, if the anchor is unusable, abstain; never fall back to an older valid
    book or a post-C snapshot. With `scheduled_start`, the book's own
    eligibility proof must carry that start: a book collected under a different
    schedule version is `book_schedule_mismatch` (specification §2 rule 5, §4).
    Arithmetic-level book reasons (side missing, crossed, flagged) are left to
    baselines.midpoint on the returned raw payload.
    """
    from . import evidence  # lazy: keep this module import-pure (no sqlite3 at import)

    if version not in SELECTION_VERSIONS:
        raise ValueError(f"unknown selection version {version!r}")
    cutoff = utc(cutoff)
    identifier(ticker)
    rows = db.execute(
        "SELECT f.id, f.seq, f.state, f.retrieved_at, f.truncated FROM fetches f"
        " JOIN requests r ON r.id=f.request_id"
        " WHERE r.stage='book' AND r.request_key LIKE ? AND f.retrieved_at IS NOT NULL"
        " AND f.state IN ('ok','parse_error','error') AND f.retrieved_at<=?"
        " ORDER BY f.retrieved_at DESC, f.seq DESC LIMIT ?",
        ("%" + ticker, cutoff.isoformat(), 1 if version == SELECTION_V1 else V2_SCAN_LIMIT),
    ).fetchall()
    if not rows:
        return InputsOrAbstention(None, ("book_missing",))
    row = rows[0]
    availability = evidence.fetch_availability(db, row[0], namespace=namespace, mode=mode)
    if version == SELECTION_V2:
        # Availability, never content, decides the anchor: the scan skips only attempts
        # not yet witnessed by C, and whatever it lands on is checked below as is.
        for candidate in rows:
            found = evidence.fetch_availability(db, candidate[0], namespace=namespace, mode=mode)
            if found.status == "witnessed" and found.observed_at <= cutoff:
                row, availability = candidate, found
                break
    fetch_id, _seq, state, retrieved_at, truncated = row
    if availability.status == "availability_unproven":
        return InputsOrAbstention(None, ("book_availability_unproven",))
    if availability.status == "clock_untrusted":
        return InputsOrAbstention(None, ("book_clock_untrusted",))
    if availability.observed_at is None or availability.observed_at > cutoff:
        return InputsOrAbstention(None, ("book_availability_after_cutoff",))
    if truncated:
        return InputsOrAbstention(None, ("book_truncated",))
    if state == "error":
        return InputsOrAbstention(None, ("book_fetch_failed",))
    if state == "parse_error":
        return InputsOrAbstention(None, ("book_parse_error",))
    age = (cutoff - utc(datetime.fromisoformat(retrieved_at))).total_seconds()
    if age > 300:
        return InputsOrAbstention(None, ("book_stale",))
    observation = db.execute(
        "SELECT o.data_json FROM observations o WHERE o.fetch_id=? AND o.kind='book'"
        " AND o.ticker=? LIMIT 1",
        (fetch_id, ticker),
    ).fetchone()
    if observation is None:
        return InputsOrAbstention(None, ("book_parse_error",))
    parsed = json.loads(observation[0])
    if game_id is not None and parsed.get("game_id") != game_id:
        return InputsOrAbstention(None, ("book_schedule_mismatch",))
    if scheduled_start is not None:
        proof = db.execute(
            "SELECT json_extract(data_json,'$.scheduled_start') FROM eligibility WHERE id=?",
            (parsed.get("eligibility_id"),),
        ).fetchone()
        if (
            proof is None
            or proof[0] is None
            or utc(datetime.fromisoformat(proof[0])) != utc(datetime.fromisoformat(scheduled_start))
        ):
            return InputsOrAbstention(None, ("book_schedule_mismatch",))
    blob = db.execute(
        "SELECT b.body FROM fetches f JOIN blobs b ON b.sha256=f.body_sha256 WHERE f.id=?",
        (fetch_id,),
    ).fetchone()
    if blob is None:
        return InputsOrAbstention(None, ("archive_corrupt",))
    return InputsOrAbstention(
        BookInput(
            fetch_id=fetch_id,
            retrieved_at=datetime.fromisoformat(retrieved_at),
            observed_at=availability.observed_at,
            receipt_id=availability.receipt_id,
            raw=json.loads(blob[0]),
        ),
        (),
    )
