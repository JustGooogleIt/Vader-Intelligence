"""Operational publication verdict and record (specification §2, §6).

Publication is the third, independent decision: it records when a frozen cutoff result
was actually released, and never edits the cutoff probabilities. The verdict comes from
the pure `policy.classify_publication`; this module only shapes the forecast_publications
envelope. The allowed record's post-commit witness (a later receipt) is what proves
`published_timely`; this layer does not witness.
"""

from .policy import classify_publication

# policy status -> stored verdict. The journal stores one of allowed/vetoed/missed/failed;
# timeliness (published_timely / late_publication / publication_unconfirmed) is derived
# later from the allowed record plus its witnessed receipt.
VERDICT = {
    "published_timely": "allowed",
    "late_publication": "allowed",
    "publication_unconfirmed": "allowed",
    "vetoed": "vetoed",
    "missed": "missed",
    "not_attempted": "missed",
    "failed": "failed",
}


def decide(
    *,
    scheduled_start,
    attempted_at,
    published_at=None,
    veto_observed_at=None,
    veto_reason=None,
    failed=False,
    clock_trusted=True,
    mode="forward_shadow",
):
    """Classify the attempt. Returns the pure Publication plus the stored verdict."""
    classification = classify_publication(
        scheduled_start=scheduled_start,
        attempted_at=attempted_at,
        published_at=published_at,
        veto_observed_at=veto_observed_at,
        veto_reason=veto_reason,
        failed=failed,
        clock_trusted=clock_trusted,
        mode=mode,
    )
    return classification, VERDICT[classification.status]


def envelope(
    common,
    *,
    decision_id,
    decision_digest,
    attempted_at,
    classification,
    verdict,
    publication_policy,
    references=(),
    extra=None,
):
    """Build the forecast_publications record. `common` carries schema/run/code/config/mode."""
    reasons = [] if verdict == "allowed" else [classification.reason or verdict]
    manifest = {
        "policy_status": classification.status,
        "delay_seconds": None
        if classification.delay is None
        else str(classification.delay.total_seconds()),
    }
    if extra:
        manifest.update(extra)
    return common | {
        "decision_id": decision_id,
        "decision_digest": decision_digest,
        "attempted_at": attempted_at,
        "verdict": verdict,
        "publication_policy": publication_policy,
        "reasons": reasons,
        "manifest": manifest,
        "references": list(references),
    }
