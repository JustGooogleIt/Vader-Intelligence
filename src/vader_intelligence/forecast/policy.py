"""Pure v1 timestamp predicates. Supplied observations are assertions, not proof.

No function reads a clock, chooses schedule history, creates a receipt or publishes.
The evidence layer must choose the correct sticky cutoff and latest pre-cutoff inputs.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

HORIZON = timedelta(hours=1)
MAX_AGE = timedelta(seconds=300)
PUBLICATION_GRACE = timedelta(seconds=150)


def utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timestamp must be an aware datetime")
    return value.astimezone(timezone.utc)


def decision_cutoff(scheduled_start):
    return utc(scheduled_start) - HORIZON


@dataclass(frozen=True)
class EvidenceTimes:
    retrieved_at: datetime
    observed_at: datetime | None


@dataclass(frozen=True)
class Eligibility:
    reasons: tuple[str, ...]

    @property
    def eligible(self):
        return not self.reasons


def freshness(evidence, cutoff):
    """Check supplied retrieval/visibility bounds; 0..300 seconds is inclusive."""
    cutoff = utc(cutoff)
    if evidence is None:
        return Eligibility(("input_missing",))
    if not isinstance(evidence, EvidenceTimes):
        raise ValueError("expected EvidenceTimes or None")
    retrieved = utc(evidence.retrieved_at)
    reasons = []
    if retrieved > cutoff:
        reasons.append("input_after_cutoff")
    elif cutoff - retrieved > MAX_AGE:
        reasons.append("input_stale")
    if evidence.observed_at is None:
        reasons.append("availability_unproven")
    else:
        observed = utc(evidence.observed_at)
        if observed < retrieved:
            reasons.append("clock_untrusted")
        if observed > cutoff:
            reasons.append("availability_after_cutoff")
    return Eligibility(tuple(reasons))


def cutoff_eligibility(
    *,
    cutoff,
    binding_observed_at,
    discovery,
    schedule,
    market,
    universe_complete,
    identity_proven,
    pregame,
    contract_valid,
    clock_trusted,
):
    """Combine caller-established common predicates, never book/publication outcomes.

    discovery uses phase completion as retrieved_at. contract_valid covers open
    status/$1/rules AFTER selection. All booleans must be explicit, not truthy values.
    This checks assertions only, not their provenance or the schedule state machine.
    """
    cutoff = utc(cutoff)
    assertions = (universe_complete, identity_proven, pregame, contract_valid, clock_trusted)
    if any(type(value) is not bool for value in assertions):
        raise ValueError("eligibility assertions must be explicit booleans")
    checks = {
        name: freshness(value, cutoff)
        for name, value in (("discovery", discovery), ("schedule", schedule), ("market", market))
    }
    reasons = []
    if not clock_trusted:
        reasons.append("clock_untrusted")
    # Availability/clock comes first, then universe, identity, schedule/status.
    for name, check in checks.items():
        reasons.extend(f"{name}:{r}" for r in check.reasons if r != "input_stale")
    if binding_observed_at is None:
        reasons.append("binding_availability_unproven")
    elif utc(binding_observed_at) > cutoff:
        reasons.append("binding_after_cutoff")
    if not universe_complete or not checks["discovery"].eligible:
        reasons.append("universe_incomplete")
    if "input_stale" in checks["discovery"].reasons:
        reasons.append("discovery:input_stale")
    if not identity_proven:
        reasons.append("identity_unproven")
    for name in ("schedule", "market"):
        if "input_stale" in checks[name].reasons:
            reasons.append(name + ":input_stale")
    if not pregame:
        reasons.append("not_pregame")
    if not contract_valid:
        reasons.append("contract_ineligible")
    return Eligibility(tuple(reasons))


@dataclass(frozen=True)
class Publication:
    status: str
    reason: str | None = None
    delay: timedelta | None = None


def classify_publication(
    *,
    scheduled_start,
    attempted_at=None,
    published_at=None,
    veto_observed_at=None,
    veto_reason=None,
    failed=False,
    clock_trusted=True,
    mode="forward_shadow",
):
    """Classify supplied operational facts, without changing a cutoff decision.

    attempted_at is the final pre-commit operational check, not computation start.
    published_at is the caller's post-commit witness, not insertion/HTTP time.
    A later warning cannot revoke earlier publication. Caller enforces immutable
    first-attempt identity, live/synthetic lineage and durable receipt authenticity.
    """
    start = utc(scheduled_start)
    cutoff = decision_cutoff(start)
    if mode not in ("forward_shadow", "historical_reconstruction", "synthetic"):
        raise ValueError("unknown mode")
    if type(failed) is not bool or type(clock_trusted) is not bool:
        raise ValueError("publication assertions must be explicit booleans")
    if (veto_observed_at is None) != (veto_reason is None):
        raise ValueError("veto requires both observation time and reason")
    if veto_reason is not None and (not isinstance(veto_reason, str) or not veto_reason.strip()):
        raise ValueError("veto reason must be a nonempty string")
    veto = utc(veto_observed_at) if veto_observed_at is not None else None
    attempted = utc(attempted_at) if attempted_at is not None else None
    published = utc(published_at) if published_at is not None else None
    if mode == "historical_reconstruction":
        if attempted is not None or published is not None or failed or veto is not None:
            raise ValueError("reconstruction cannot relabel operational publication facts")
        return Publication("not_applicable")
    if attempted is None:
        if published is not None or failed:
            raise ValueError("publication/failure requires an attempt")
        return Publication("not_attempted")
    if published is not None and (published < attempted or attempted < cutoff or failed):
        raise ValueError("inconsistent publication chronology or failure")
    if not clock_trusted:
        return Publication("failed", "clock_untrusted")
    if attempted < cutoff:
        return Publication("not_attempted", "before_cutoff")
    if attempted > cutoff + PUBLICATION_GRACE or attempted >= start:
        return Publication("missed", "late_invocation")
    if veto is not None and veto <= attempted:
        return Publication("vetoed", veto_reason)
    if failed:
        return Publication("failed", "publication_failed")
    if published is None:
        return Publication("publication_unconfirmed")
    delay = published - cutoff
    if published > cutoff + PUBLICATION_GRACE or published >= start:
        return Publication("late_publication", "publication_outside_window", delay)
    return Publication("published_timely", delay=delay)
