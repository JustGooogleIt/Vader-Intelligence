"""Pregame eligibility for one NFL/NBA candidate against one schedule snapshot (v1).

Pure: every time is passed in. Reasons are checked in a fixed order and the first
applicable one is returned, like `scope.eligible` for MLB. The schedule source's own
content time bounds how current its status can be (D-020); the retrieval age is checked
separately, so a freshly retrieved but stale file is still refused.
"""

from dataclasses import dataclass
from datetime import timedelta

from .schedules import EASTERN

PREGAME_BUFFER = timedelta(seconds=120)
ELIGIBLE_SEASON_TYPES = ("REG", "POST")


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str
    game_id: str | None = None
    start: str | None = None
    cutoff: str | None = None  # book-request cutoff: start − buffer
    neutral: bool | None = None


def _reject(reason, game=None):
    return Eligibility(False, reason, game.game_id if game else None)


def eligible(
    league,
    candidate,
    games,
    *,
    content_time,
    retrieved_at,
    now,
    retrieval_max_age=timedelta(seconds=120),
    pregame_buffer=PREGAME_BUFFER,
):
    if pregame_buffer < PREGAME_BUFFER:
        raise ValueError("pregame buffer below 120 seconds")
    if content_time is None or not timedelta(0) <= now - content_time <= league.content_max_age:
        return _reject("schedule_content_stale")
    if retrieved_at is None or not timedelta(0) <= now - retrieved_at <= retrieval_max_age:
        return _reject("schedule_retrieval_stale")
    matches = [
        g
        for g in games
        if g.et_date == candidate.et_date
        and g.away == candidate.away.source
        and g.home == candidate.home.source
    ]
    if not matches:
        return _reject("unmapped_game")
    if len(matches) > 1:
        return _reject("ambiguous_game")
    game = matches[0]
    if game.season_type not in ELIGIBLE_SEASON_TYPES:
        return _reject("unsupported_season_type", game)
    if game.start is None:
        return _reject("start_time_unknown", game)
    if game.start.astimezone(EASTERN).date() != candidate.et_date:
        return _reject("schedule_date_mismatch", game)
    if game.state != "scheduled":
        return _reject("not_scheduled_pregame", game)
    cutoff = game.start - pregame_buffer
    if now >= cutoff:
        return Eligibility(
            False,
            "past_pregame_cutoff",
            game.game_id,
            game.start.isoformat(),
            cutoff.isoformat(),
            game.neutral,
        )
    return Eligibility(
        True, "eligible", game.game_id, game.start.isoformat(), cutoff.isoformat(), game.neutral
    )
