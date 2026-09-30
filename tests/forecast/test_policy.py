"""All timestamps and assertions are synthetic; no receipts/publication are created."""

from datetime import datetime, timedelta, timezone

import pytest

from vader_intelligence.forecast.baselines import constant
from vader_intelligence.forecast.policy import (
    EvidenceTimes,
    classify_publication,
    cutoff_eligibility,
    decision_cutoff,
    freshness,
)

T = datetime(2026, 10, 1, 20, tzinfo=timezone.utc)
C = T - timedelta(hours=1)
US = timedelta(microseconds=1)


def inputs():
    evidence = EvidenceTimes(C - timedelta(seconds=60), C - timedelta(seconds=30))
    return dict(
        cutoff=C,
        binding_observed_at=C - timedelta(seconds=30),
        discovery=evidence,
        schedule=evidence,
        market=evidence,
        universe_complete=True,
        identity_proven=True,
        pregame=True,
        contract_valid=True,
        clock_trusted=True,
    )


def test_cutoff_and_offsets_are_utc_instants():
    assert decision_cutoff(T) == C
    assert decision_cutoff(T.astimezone(timezone(timedelta(hours=-7)))) == C
    with pytest.raises(ValueError):
        decision_cutoff(T.replace(tzinfo=None))


@pytest.mark.parametrize(
    "age,eligible",
    [
        (timedelta(), True),
        (timedelta(seconds=300), True),
        (timedelta(seconds=300) + US, False),
        (timedelta(seconds=350), False),
        (-US, False),
    ],
)
def test_freshness_boundaries(age, eligible):
    retrieved = C - age
    result = freshness(EvidenceTimes(retrieved, max(C, retrieved)), C)
    assert result.eligible is eligible


def test_visibility_and_missing_evidence_fail_closed():
    assert freshness(None, C).reasons == ("input_missing",)
    assert freshness(EvidenceTimes(C, None), C).reasons == ("availability_unproven",)
    assert freshness(EvidenceTimes(C, C + US), C).reasons == ("availability_after_cutoff",)
    assert freshness(EvidenceTimes(C, C - US), C).reasons == ("clock_untrusted",)
    with pytest.raises(ValueError):
        freshness(EvidenceTimes(C.replace(tzinfo=None), C), C)


def test_350_second_example_and_no_post_cutoff_witness_repair():
    # C=19:00; skip 18:56 collection. 18:54:10 book was witnessed at 18:56.
    old = EvidenceTimes(C - timedelta(seconds=350), C - timedelta(minutes=4))
    assert freshness(old, C).reasons == ("input_stale",)
    # 18:58:10 collection is fresh but first witnessed at 19:00:01.
    replacement = EvidenceTimes(C - timedelta(seconds=110), C + timedelta(seconds=1))
    assert freshness(replacement, C).reasons == ("availability_after_cutoff",)


@pytest.mark.parametrize("offset", [0, 1, 30, 60, 119])
@pytest.mark.parametrize("runtime", [10, 90])
@pytest.mark.parametrize("missed", [False, True])
def test_nominal_cycle_phase_sweep(offset, runtime, missed):
    # Latest pre-C tick reads an older pass, never the following collection.
    tick = C - timedelta(seconds=offset)
    started = tick - timedelta(seconds=240 if missed else 120)
    evidence = EvidenceTimes(started + timedelta(seconds=runtime), tick)
    assert freshness(evidence, C).eligible == (offset + (240 if missed else 120) - runtime <= 300)


def test_cutoff_and_publication_veto_are_independent():
    frozen = cutoff_eligibility(**inputs())
    assert frozen.eligible and constant().as_tuple().digits == (5,)
    warning = C + timedelta(seconds=30)
    early = classify_publication(
        scheduled_start=T,
        attempted_at=C + timedelta(seconds=10),
        published_at=C + timedelta(seconds=11),
        veto_observed_at=warning,
        veto_reason="postponed",
    )
    late = classify_publication(
        scheduled_start=T,
        attempted_at=C + timedelta(seconds=60),
        veto_observed_at=warning,
        veto_reason="postponed",
    )
    assert early.status == "published_timely" and late.status == "vetoed"
    assert cutoff_eligibility(**inputs()) == frozen  # Same information population.
    assert (
        classify_publication(scheduled_start=T, mode="historical_reconstruction").status
        == "not_applicable"
    )


@pytest.mark.parametrize(
    "field,reason",
    [
        ("universe_complete", "universe_incomplete"),
        ("identity_proven", "identity_unproven"),
        ("pregame", "not_pregame"),
        ("contract_valid", "contract_ineligible"),
        ("clock_trusted", "clock_untrusted"),
    ],
)
def test_common_checks_are_explicit(field, reason):
    values = inputs()
    values[field] = False
    assert reason in cutoff_eligibility(**values).reasons
    values[field] = "false"
    with pytest.raises(ValueError):
        cutoff_eligibility(**values)


def test_common_inputs_do_not_accept_missing_or_future_binding():
    assert not cutoff_eligibility(**dict(inputs(), binding_observed_at=None)).eligible
    assert not cutoff_eligibility(**dict(inputs(), binding_observed_at=C + US)).eligible
    assert not cutoff_eligibility(**dict(inputs(), schedule=None)).eligible
    assert not cutoff_eligibility(**dict(inputs(), discovery=None)).eligible
    stale = EvidenceTimes(C - timedelta(seconds=301), C)
    assert "market:input_stale" in cutoff_eligibility(**dict(inputs(), market=stale)).reasons


@pytest.mark.parametrize(
    "attempt,published,status",
    [
        (None, None, "not_attempted"),
        (-US, None, "not_attempted"),
        (timedelta(), timedelta(), "published_timely"),
        (timedelta(seconds=150), timedelta(seconds=150), "published_timely"),
        (timedelta(seconds=150) + US, None, "missed"),
        (timedelta(seconds=149), timedelta(seconds=150) + US, "late_publication"),
        (timedelta(seconds=149), timedelta(hours=1), "late_publication"),
        (timedelta(seconds=1), None, "publication_unconfirmed"),
    ],
)
def test_publication_boundaries(attempt, published, status):
    result = classify_publication(
        scheduled_start=T,
        attempted_at=None if attempt is None else C + attempt,
        published_at=None if published is None else C + published,
    )
    assert result.status == status
    if published is not None:
        assert result.delay == published


def test_failure_clock_and_invalid_operational_facts():
    assert classify_publication(scheduled_start=T, attempted_at=C, failed=True).status == "failed"
    assert (
        classify_publication(scheduled_start=T, attempted_at=C, clock_trusted=False).reason
        == "clock_untrusted"
    )
    for kwargs in (
        {"published_at": C},
        {"attempted_at": C, "published_at": C - US},
        {"veto_reason": "postponed"},
        {"mode": "unknown"},
        {"failed": "false"},
        {"mode": "historical_reconstruction", "attempted_at": C},
        {"attempted_at": C, "published_at": C, "failed": True},
    ):
        with pytest.raises(ValueError):
            classify_publication(scheduled_start=T, **kwargs)
