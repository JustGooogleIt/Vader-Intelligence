"""Evaluation under a selection-v2 protocol (decision D-016). All evidence is synthetic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forecast"))
from _f34 import GAME_ID, at, collect, protocol, tick_at, to_schema4
from conftest import Clock
from test_materialization import settle, settlement_evidence

from vader_intelligence.evaluation.service import evaluate
from vader_intelligence.forecast import service
from vader_intelligence.forecast.research import Protocol
from vader_intelligence.forecast.selection import SELECTION_V1, SELECTION_V2

V1 = protocol()
V2 = Protocol(dataset=V1.dataset, selection_version=SELECTION_V2)


def cycles(store, config, proto, *, decide):
    to_schema4(store)
    for minute in (1, 3):
        tick_at(store, proto, at(18, minute, 0))
        collect(store, config, origin=at(18, minute, 1))
    if decide:
        tick_at(store, proto, at(18, 5, 10))


def evaluate_with(store, proto, moment):
    clock = Clock()
    clock.origin = moment
    return evaluate(
        store,
        proto,
        clock=clock.now,
        monotonic=clock.mono,
        outcomes_as_of=at(23, 30),
        view="forward-shadow",
    )["report"]


def test_a_v2_cohort_scores_the_midpoint_and_stays_separate(store, config, monkeypatch):
    cycles(store, config, V2, decide=True)
    settle(store, config, monkeypatch, settlement_evidence())
    report = evaluate_with(store, V2, at(23, 40))
    assert report["population"]["M"] == 1
    # The v1 protocol is another cohort: it sees none of the v2 decisions.
    other = evaluate_with(store, V1, at(23, 41))
    assert other["population"]["M"] == 0


def test_unattempted_annotations_use_the_protocol_version(store, config, monkeypatch):
    # A binding prepared but never decided: evaluation annotates the opportunity with the
    # cutoff facts recomputed under the evaluating protocol's selection version.
    cycles(store, config, V2, decide=False)
    settle(store, config, monkeypatch, settlement_evidence())
    report = evaluate_with(store, V2, at(23, 40))
    annotation = report["annotations"][f"game:{GAME_ID}"]["cutoff_policy_annotation"]
    assert annotation["midpoint_available"] is True and annotation["midpoint_reasons"] == []
    # The v1 rule on the same binding abstains, so the annotation came from v2.
    binding = service.load_binding(store.db, annotation["binding_id"])
    v1 = service.cutoff_facts(
        store.db,
        binding=binding,
        namespace=V1.observer_namespace,
        evidence_mode="synthetic",
        selection_version=SELECTION_V1,
    )
    assert v1.midpoint_probability is None
