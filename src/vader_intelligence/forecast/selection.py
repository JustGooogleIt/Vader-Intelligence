"""Choose within one already identity-qualified game's candidates, without hindsight."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..normalize import identifier


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
