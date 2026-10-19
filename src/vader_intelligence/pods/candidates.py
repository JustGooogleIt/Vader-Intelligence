"""Candidate lifecycle (G2): proposed → validated → shadow_tested → paper_approved.

Side exits: rejected, quarantined, retired. Every transition goes through `promote()`,
which requires exactly the typed evidence records for that edge, checks the promotion
criteria, and writes evidence, a transition event and the new state in one transaction.
The schema refuses illegal edges and state changes without their event, so skipping a
state is impossible even through raw SQL.

Evidence comes from the protected evaluator, paper-execution reports and an operator,
all in the coordinator process; workers have no code path to these tables. No state
here names or implies live eligibility: live trading requires independent review and
explicit user activation outside the pod system.

The holdout ledger records each candidate's final-holdout evaluation at most once;
consumption is permanent (append-only table) and is always reported.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar

from ..provenance import json_text
from .contracts import (
    ALLOWED_SPLITS,
    ARTIFACT_KIND,
    HEX32,
    HEX64,
    POD_ID,
    ContractError,
    content_hash,
    normalize_fields,
    require,
    to_plain,
)
from .store import OrchestrationStore, epoch_seconds, iso

CANDIDATE_STATES = (
    "proposed",
    "validated",
    "shadow_tested",
    "paper_approved",
    "rejected",
    "quarantined",
    "retired",
)
EDGES = {
    ("proposed", "validated"): ("evaluator_metrics",),
    ("validated", "shadow_tested"): ("shadow_evaluation",),
    ("shadow_tested", "paper_approved"): ("operator_approval", "paper_report"),
}
EXITS = {
    "rejected": ("proposed", "validated", "shadow_tested"),
    "quarantined": ("proposed", "validated", "shadow_tested", "paper_approved"),
    "retired": ("validated", "shadow_tested", "paper_approved"),
}
HOLDOUT_STATES = ("validated", "shadow_tested", "paper_approved")
EVALUATOR = "protected_evaluator"


class PromotionRefused(ValueError):
    """The requested transition, its evidence, or the evidence's criteria are not met."""


class HoldoutAlreadyConsumed(ValueError):
    pass


def _nonempty(value: str, what: str) -> None:
    require(0 < len(value) <= 256, f"{what} must be 1..256 characters")


def _candidate(value: str) -> None:
    require(bool(HEX32.match(value)), "candidate_id must be 32 hex characters")


def _period(start: str, end: str) -> None:
    try:
        require(date.fromisoformat(start) <= date.fromisoformat(end), "period must be ordered")
    except ValueError:
        raise ContractError("period dates must be ISO dates") from None


@dataclass(frozen=True)
class CandidateSpec:
    """A proposal derived from one accepted pod result (its full provenance chain)."""

    pod_id: str
    kind: str
    spec: dict[str, Any]
    source_task_id: str

    def __post_init__(self):
        normalize_fields(self)
        require(bool(POD_ID.match(self.pod_id)), "invalid pod_id")
        require(bool(ARTIFACT_KIND.match(self.kind)), "invalid candidate kind")
        require(bool(HEX32.match(self.source_task_id)), "invalid source_task_id")


@dataclass(frozen=True)
class EvaluatorMetricRecord:
    """Development/validation metrics computed by the protected evaluator, not a pod."""

    KIND: ClassVar[str] = "evaluator_metrics"
    candidate_id: str
    evaluator_version: str
    source: str
    dataset_manifest_hash: str
    splits: tuple[str, ...]
    metric: str
    lower_is_better: bool
    candidate_value: float
    baseline_id: str
    baseline_value: float
    paired_n: int
    min_n: int
    stability_passed: bool
    search_budget: dict[str, Any]
    candidate_history_hash: str

    def __post_init__(self):
        normalize_fields(self)
        _candidate(self.candidate_id)
        _nonempty(self.evaluator_version, "evaluator_version")
        require(self.source == EVALUATOR, f"source must be {EVALUATOR!r}")
        require(bool(HEX64.match(self.dataset_manifest_hash)), "dataset_manifest_hash must be hex")
        require(bool(self.splits), "splits required")
        require(
            set(self.splits) <= set(ALLOWED_SPLITS), "splits may only be development/validation"
        )
        _nonempty(self.metric, "metric")
        _nonempty(self.baseline_id, "baseline_id")
        require(self.paired_n >= 0 and self.min_n >= 1, "paired_n >= 0 and min_n >= 1 required")
        require(
            bool(HEX64.match(self.candidate_history_hash)), "candidate_history_hash must be hex"
        )

    def check(self):
        if self.paired_n < self.min_n:
            raise PromotionRefused(f"paired N {self.paired_n} below min_n {self.min_n}")
        if self.lower_is_better:
            beats = self.candidate_value < self.baseline_value
        else:
            beats = self.candidate_value > self.baseline_value
        if not beats:
            raise PromotionRefused("candidate does not beat the declared baseline")
        if not self.stability_passed:
            raise PromotionRefused("stability checks failed")
        if not self.search_budget:
            raise PromotionRefused("search budget must be attached")


@dataclass(frozen=True)
class ShadowEvaluationRecord:
    """Forward-shadow (or explicitly labelled reconstruction) evaluation with coverage."""

    KIND: ClassVar[str] = "shadow_evaluation"
    candidate_id: str
    forecaster_id: str
    evaluation_id: str
    mode: str
    coverage: float
    n_forecasts: int
    period_start: str
    period_end: str

    def __post_init__(self):
        normalize_fields(self)
        _candidate(self.candidate_id)
        _nonempty(self.forecaster_id, "forecaster_id")
        _nonempty(self.evaluation_id, "evaluation_id")
        require(self.mode in ("forward_shadow", "reconstruction"), "mode must be labelled")
        require(0.0 <= self.coverage <= 1.0, "coverage must be in [0, 1]")
        require(self.n_forecasts >= 0, "n_forecasts must be non-negative")
        _period(self.period_start, self.period_end)

    def check(self):
        if self.n_forecasts < 1 or self.coverage <= 0.0:
            raise PromotionRefused("shadow evaluation has no covered forecasts")


@dataclass(frozen=True)
class PaperReportRecord:
    """Paper-execution report under conservative assumptions with no risk-check bypass."""

    KIND: ClassVar[str] = "paper_report"
    candidate_id: str
    report_id: str
    execution_assumptions: str
    risk_checks_bypassed: int
    n_decisions: int
    period_start: str
    period_end: str

    def __post_init__(self):
        normalize_fields(self)
        _candidate(self.candidate_id)
        _nonempty(self.report_id, "report_id")
        _nonempty(self.execution_assumptions, "execution_assumptions")
        require(self.risk_checks_bypassed >= 0, "risk_checks_bypassed must be non-negative")
        require(self.n_decisions >= 0, "n_decisions must be non-negative")
        _period(self.period_start, self.period_end)

    def check(self):
        if self.execution_assumptions != "conservative":
            raise PromotionRefused("paper report must use conservative execution assumptions")
        if self.risk_checks_bypassed:
            raise PromotionRefused("paper report shows bypassed risk checks")
        if self.n_decisions < 1:
            raise PromotionRefused("paper report has no decisions")


@dataclass(frozen=True)
class OperatorApprovalRecord:
    """An operator's approval naming the candidate; the only scope that exists is paper."""

    KIND: ClassVar[str] = "operator_approval"
    candidate_id: str
    operator: str
    approved_at: str
    statement: str
    scope: str = "paper"

    def __post_init__(self):
        normalize_fields(self)
        _candidate(self.candidate_id)
        _nonempty(self.operator, "operator")
        _nonempty(self.statement, "statement")
        require(self.scope == "paper", "approval scope must be 'paper'")
        try:
            datetime.fromisoformat(self.approved_at)
        except ValueError:
            raise ContractError("approved_at must be an ISO timestamp") from None

    def check(self):
        pass


@dataclass(frozen=True)
class ExitDecision:
    """Reason for a side exit (rejected, quarantined, retired)."""

    KIND: ClassVar[str] = "exit_decision"
    candidate_id: str
    to_state: str
    reason: str
    decided_by: str

    def __post_init__(self):
        normalize_fields(self)
        _candidate(self.candidate_id)
        require(self.to_state in EXITS, f"exit state must be one of {sorted(EXITS)}")
        _nonempty(self.reason, "reason")
        _nonempty(self.decided_by, "decided_by")

    def check(self):
        pass


@dataclass(frozen=True)
class HoldoutEvaluationRecord:
    """The one final-holdout evaluation of a candidate, by the protected evaluator."""

    candidate_id: str
    evaluator_version: str
    source: str
    dataset_manifest_hash: str
    metric: str
    value: float
    n: int

    def __post_init__(self):
        normalize_fields(self)
        _candidate(self.candidate_id)
        _nonempty(self.evaluator_version, "evaluator_version")
        require(self.source == EVALUATOR, f"source must be {EVALUATOR!r}")
        require(bool(HEX64.match(self.dataset_manifest_hash)), "dataset_manifest_hash must be hex")
        _nonempty(self.metric, "metric")
        require(self.n >= 1, "n must be positive")


EVIDENCE_TYPES = (
    EvaluatorMetricRecord,
    ShadowEvaluationRecord,
    PaperReportRecord,
    OperatorApprovalRecord,
    ExitDecision,
)


def propose_candidate(store: OrchestrationStore, spec: CandidateSpec, now=None) -> str:
    """Register a candidate from an accepted result of the same pod; idempotent."""
    require(isinstance(spec, CandidateSpec), "spec must be a CandidateSpec")
    identity = {
        "pod_id": spec.pod_id,
        "kind": spec.kind,
        "spec": spec.spec,
        "source_task_id": spec.source_task_id,
    }
    candidate_id = content_hash(identity)[:32]
    at = epoch_seconds(now)
    with store.transaction():
        source = store.db.execute(
            "SELECT t.pod_id, t.experiment_manifest_hash FROM tasks t "
            "JOIN results r USING(task_id) WHERE t.task_id=?",
            (spec.source_task_id,),
        ).fetchone()
        require(source is not None, "source task has no accepted result")
        require(source["pod_id"] == spec.pod_id, "source task belongs to another pod")
        exists = store.db.execute(
            "SELECT 1 FROM candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if exists:
            return candidate_id
        store.db.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,'proposed')",
            (
                candidate_id,
                spec.pod_id,
                spec.kind,
                json_text(spec.spec),
                content_hash(spec.spec),
                spec.source_task_id,
                source["experiment_manifest_hash"],
                at,
            ),
        )
        store.db.execute(
            "INSERT INTO candidate_events(candidate_id, from_state, to_state, evidence_ids_json, "
            "at) VALUES (?, NULL, 'proposed', '[]', ?)",
            (candidate_id, at),
        )
    return candidate_id


def promote(store: OrchestrationStore, candidate_id, to_state, evidence, now=None) -> str:
    """The only way a candidate changes state.

    Requires the current state to have an edge to `to_state` (no skipping), exactly one
    typed record of each kind that edge requires (nothing else is accepted), every
    record naming this candidate, and every record's criteria to pass.
    """
    evidence = tuple(evidence)
    for item in evidence:
        if type(item) not in EVIDENCE_TYPES:
            raise PromotionRefused(f"untyped or unknown evidence: {type(item).__name__}")
    at = epoch_seconds(now)
    with store.transaction():
        row = store.db.execute(
            "SELECT state FROM candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        current = row["state"]
        if to_state in EXITS:
            if current not in EXITS[to_state]:
                raise PromotionRefused(f"{current} cannot exit to {to_state}")
            required = ("exit_decision",)
        else:
            required = EDGES.get((current, to_state))
            if required is None:
                raise PromotionRefused(f"no transition {current} → {to_state}")
        kinds = sorted(item.KIND for item in evidence)
        if kinds != sorted(required):
            raise PromotionRefused(f"{current} → {to_state} requires exactly {list(required)}")
        for item in evidence:
            if item.candidate_id != candidate_id:
                raise PromotionRefused("evidence names another candidate")
            if isinstance(item, ExitDecision) and item.to_state != to_state:
                raise PromotionRefused("exit decision names another state")
            item.check()
        evidence_ids = []
        for item in evidence:
            payload = to_plain(item)
            evidence_id = content_hash({"kind": item.KIND, "payload": payload})
            store.db.execute(
                "INSERT INTO candidate_evidence VALUES (?,?,?,?,?) "
                "ON CONFLICT(evidence_id) DO NOTHING",
                (evidence_id, candidate_id, item.KIND, json_text(payload), at),
            )
            evidence_ids.append(evidence_id)
        store.db.execute(
            "INSERT INTO candidate_events(candidate_id, from_state, to_state, evidence_ids_json, "
            "at) VALUES (?,?,?,?,?)",
            (candidate_id, current, to_state, json_text(evidence_ids), at),
        )
        store.db.execute(
            "UPDATE candidates SET state=? WHERE candidate_id=?", (to_state, candidate_id)
        )
    return to_state


def record_holdout_evaluation(store: OrchestrationStore, record, now=None) -> str:
    """Consume a candidate's final holdout exactly once; returns the evaluation hash."""
    require(isinstance(record, HoldoutEvaluationRecord), "record must be HoldoutEvaluationRecord")
    payload = to_plain(record)
    evaluation_hash = content_hash(payload)
    with store.transaction():
        row = store.db.execute(
            "SELECT state FROM candidates WHERE candidate_id=?", (record.candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(record.candidate_id)
        consumed = store.db.execute(
            "SELECT consumed_at FROM holdout_ledger WHERE candidate_id=?", (record.candidate_id,)
        ).fetchone()
        if consumed is not None:
            raise HoldoutAlreadyConsumed(
                f"holdout for {record.candidate_id} was consumed at {iso(consumed[0])}"
            )
        if row["state"] not in HOLDOUT_STATES:
            raise PromotionRefused(f"holdout may be evaluated only in {list(HOLDOUT_STATES)}")
        store.db.execute(
            "INSERT INTO holdout_ledger VALUES (?,?,?,?,?)",
            (
                record.candidate_id,
                record.evaluator_version,
                json_text(payload),
                evaluation_hash,
                epoch_seconds(now),
            ),
        )
    return evaluation_hash


def holdout_status(store: OrchestrationStore, candidate_id) -> dict:
    """Whether the holdout was consumed; a consumed holdout is never reported untouched."""
    row = store.db.execute(
        "SELECT * FROM holdout_ledger WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        return {"candidate_id": candidate_id, "consumed": False}
    return {
        "candidate_id": candidate_id,
        "consumed": True,
        "consumed_at": iso(row["consumed_at"]),
        "evaluation_hash": row["evaluation_hash"],
        "evaluation": json.loads(row["evaluation_json"]),
    }


def candidate_record(store: OrchestrationStore, candidate_id) -> dict:
    """Full chain: candidate, source result and manifest, transitions, evidence, holdout."""
    row = store.db.execute(
        "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise KeyError(candidate_id)
    events = store.db.execute(
        "SELECT from_state, to_state, evidence_ids_json, at FROM candidate_events "
        "WHERE candidate_id=? ORDER BY id",
        (candidate_id,),
    ).fetchall()
    evidence = store.db.execute(
        "SELECT evidence_id, kind, payload_json FROM candidate_evidence WHERE candidate_id=? "
        "ORDER BY recorded_at, evidence_id",
        (candidate_id,),
    ).fetchall()
    result = store.result(row["source_task_id"])
    return {
        "candidate_id": candidate_id,
        "pod_id": row["pod_id"],
        "kind": row["kind"],
        "state": row["state"],
        "spec": json.loads(row["spec_json"]),
        "source_task_id": row["source_task_id"],
        "source_result_hash": result["result_hash"],
        "source_artifacts": result["result"]["artifacts"],
        "experiment_manifest_hash": row["experiment_manifest_hash"],
        "transitions": [
            {
                "from": event["from_state"],
                "to": event["to_state"],
                "evidence_ids": json.loads(event["evidence_ids_json"]),
                "at": iso(event["at"]),
            }
            for event in events
        ],
        "evidence": [
            {"evidence_id": e["evidence_id"], "kind": e["kind"], "payload": json.loads(e[2])}
            for e in evidence
        ],
        "holdout": holdout_status(store, candidate_id),
    }
