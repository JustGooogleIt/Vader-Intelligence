"""Timing sweep (F3.3 plan §6 table, cases G1 and G2) through the stored projection.

A deterministic virtual serial timeline of the one existing cycle: tick (witness) at
t_k + 1, discovery complete at t_k + 11, cycles 120 s apart. Each cell is reproduced
from real rows in a disposable schema-4 archive, not from arithmetic on the model.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forecast_storage.test_evidence import Archive  # noqa: E402

from vader_intelligence.forecast import evidence  # noqa: E402

CYCLE = 120
N = 6  # cycle index of the cutoff cycle; passes exist for cycles 0..N-1 as the scenario allows
ORIGIN = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def t(k):
    return ORIGIN + timedelta(seconds=CYCLE * k)


def build(tmp_path, *, missing_pass=(), missing_tick=(), duration=11, incomplete=()):
    # Plan §6 base timeline: observation 1 s into the cycle, discovery 10 s later.
    archive = Archive(tmp_path / "sweep.sqlite3")
    passes = {}
    for k in range(N):
        archive.run(f"run-{k}")
        archive.fetch(f"run-{k}", "status", t(k) + timedelta(seconds=1))
        if k in missing_pass:
            continue
        complete = k not in incomplete
        passes[k] = archive.pass_(
            f"run-{k}",
            t(k) + timedelta(seconds=duration),
            complete=complete,
            reasons=() if complete else ("deadline_exhausted",),
            session=f"cycle-{k}",
        )
    # Tick at t_j + 1 witnesses every pass completed before it and not yet witnessed.
    witnessed = set()
    for j in range(N + 1):
        if j in missing_tick:
            continue
        observed = t(j) + timedelta(seconds=1)
        for k, pass_id in passes.items():
            if k in witnessed:
                continue
            if t(k) + timedelta(seconds=duration) <= observed:
                archive.receipt(pass_id=pass_id, observed_at=observed)
                witnessed.add(k)
    return archive, passes


SCENARIOS = {
    "no_miss": dict(),
    "collection_missed_ticks_ran": dict(missing_pass=(N - 1,)),
    "whole_cycle_missed": dict(missing_pass=(N - 1,), missing_tick=(N - 1,)),
}
# (offset x) -> expected (pass index, age seconds, stale) per plan §6.
TABLE = {
    "no_miss": {
        0: (N - 2, 229, False),
        1: (N - 1, 110, False),
        60: (N - 1, 169, False),
        119: (N - 1, 228, False),
        120: (N - 1, 229, False),
    },
    "collection_missed_ticks_ran": {
        0: (N - 2, 229, False),
        1: (N - 2, 230, False),
        60: (N - 2, 289, False),
        119: (N - 2, 348, True),
        120: (N - 2, 349, True),
    },
    "whole_cycle_missed": {
        0: (N - 3, 349, True),
        1: (N - 2, 230, False),
        60: (N - 2, 289, False),
        119: (N - 2, 348, True),
        120: (N - 2, 349, True),
    },
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
@pytest.mark.parametrize("offset", [0, 1, 60, 119, 120])
def test_g1_applicable_pass_and_freshness_per_cell(tmp_path, scenario, offset):
    archive, passes = build(tmp_path, **SCENARIOS[scenario])
    try:
        cutoff = t(N) + timedelta(seconds=offset)
        projection = evidence.discovery_as_of(archive.db, cutoff, mode="synthetic")
        expected_pass, expected_age, stale = TABLE[scenario][offset]
        assert projection.pass_id == passes[expected_pass], (scenario, offset, projection)
        assert projection.universe_complete is True
        assert (cutoff - projection.completed_at).total_seconds() == expected_age
        reasons = projection.eligibility_reasons()
        if stale:
            assert reasons == ("universe_incomplete", "discovery:input_stale")
        else:
            assert reasons == ()
    finally:
        archive.db.close()


@pytest.mark.parametrize("duration", [0, 10, 60, 89, 91])
def test_g2_discovery_duration_shifts_age_and_91s_leaves_no_complete_summary(tmp_path, duration):
    deadline = 90
    archive, passes = build(
        tmp_path, duration=duration, incomplete=tuple(range(N)) if duration > deadline else ()
    )
    try:
        cutoff = t(N) + timedelta(seconds=1)  # the tick at t_N + 1 has just run
        projection = evidence.discovery_as_of(archive.db, cutoff, mode="synthetic")
        if duration <= 1:
            expected = N - 1  # completed at t_{N-1}+0 and witnessed by t_N+1
        else:
            expected = N - 1  # completed at t_{N-1}+duration, witnessed at t_N + 1 as well
        assert projection.pass_id == passes[expected]
        if duration > deadline:
            assert projection.universe_complete is False
            assert projection.reasons == ("deadline_exhausted",)
            assert projection.eligibility_reasons()[0] == "universe_incomplete"
        else:
            assert projection.universe_complete is True
            assert (cutoff - projection.completed_at).total_seconds() == CYCLE + 1 - duration
            assert projection.eligibility_reasons() == ()
    finally:
        archive.db.close()


def test_g3_book_outcome_cannot_appear_in_the_discovery_projection(tmp_path):
    """Holding discovery fixed, book rows and their receipts never change the projection."""
    archive, passes = build(tmp_path)
    try:
        cutoff = t(N) + timedelta(seconds=60)
        before = evidence.discovery_as_of(archive.db, cutoff, mode="synthetic")
        for state in ("ok", "error", "parse_error"):
            fetch_id, _ = archive.fetch(
                f"run-{N - 1}", "book", t(N - 1) + timedelta(seconds=30), state=state
            )
            archive.receipt(fetch_id=fetch_id, observed_at=t(N) + timedelta(seconds=1))
        after = evidence.discovery_as_of(archive.db, cutoff, mode="synthetic")
        assert (before.pass_id, before.universe_complete, before.reasons, before.completed_at) == (
            after.pass_id,
            after.universe_complete,
            after.reasons,
            after.completed_at,
        )
    finally:
        archive.db.close()
