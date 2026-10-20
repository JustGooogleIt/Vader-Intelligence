"""Schedule revisions around the cutoff (decision D-003). Synthetic throughout.

Before C a revised start makes the contract ineligible under rule v1 (stated time) and
rule v2 (ticker time), so the decision abstains with the revision reason; a revision that
is reverted before C does not taint the decision. After C the decision stands, the
publication may be vetoed, and if the settlement mapping shows the game played at another
start the opportunity is reported as `schedule_changed_after_decision` and never scored.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forecast"))
from _f34 import at, collect, fixture_data, tick_at, to_schema4
from conftest import Clock
from test_collector import handler_for
from test_eligibility_rule_v2 import V2
from test_materialization import (
    PROTO,
    evaluate_at,
    forward_slice,
    items_of,
    settle,
    settlement_evidence,
)
from test_rule_v2_pipeline import PROTO as V2_PROTO
from test_rule_v2_pipeline import dated_settlement, run_slice

from vader_intelligence.config import Config
from vader_intelligence.evaluation import outcomes
from vader_intelligence.evaluation.service import evaluate
from vader_intelligence.storage import Store


def postponed(evidence):
    """The game postponed to the next day; the provider links it to the original start."""
    evidence = copy.deepcopy(evidence)
    game = evidence["schedule"]["dates"][0]["games"][0]
    game.update(
        officialDate="2026-09-28",
        gameDate="2026-09-28T17:05:00Z",
        rescheduledFrom="2026-09-27T19:05:00Z",
        rescheduledFromDate="2026-09-27",
    )
    return evidence


def test_a_game_postponed_after_the_decision_is_reported_and_not_scored(store, config, monkeypatch):
    forward_slice(store, config)  # eligible decision at C = 18:05Z, published
    settle(store, config, monkeypatch, postponed(settlement_evidence()))
    mapping = json.loads(
        store.db.execute(
            "SELECT data_json FROM settlement_versions WHERE kind='mapping'"
        ).fetchone()[0]
    )
    assert mapping["status"] == "mapped" and mapping["reason"] == "evidenced_schedule_change"
    result = evaluate_at(store, at(23, 45), view="forward-shadow")
    report = result["report"]
    assert report["outcome_status"] == {"schedule_changed_after_decision": 1}
    assert report["outcome_reasons"] == {"start_moved:evidenced_schedule_change": 1}
    assert report["scores"]["paired"]["n"] == 0 and result["status"] == "inconclusive"
    (item,) = items_of(store, result["evaluation_id"]).values()
    assert item["y"] is None and all(v is None for v in item["scores"].values())
    assert report["evaluator_version"] == "evaluation-v2"


def test_the_same_game_at_the_decided_start_still_scores(store, config, monkeypatch):
    forward_slice(store, config)
    settle(store, config, monkeypatch, settlement_evidence())
    result = evaluate_at(store, at(23, 45), view="forward-shadow")
    assert result["report"]["outcome_status"] == {"binary_scorable": 1}
    assert result["report"]["scores"]["paired"]["n"] == 1


def test_outcome_join_without_a_decided_start_keeps_the_old_disposition(store, config, monkeypatch):
    """Annotations of games with no decided start (no binding) are unaffected."""
    forward_slice(store, config)
    settle(store, config, monkeypatch, postponed(settlement_evidence()))
    ceiling = store.db.execute("SELECT MAX(seq) FROM fetches").fetchone()[0]
    joined = outcomes.join(
        store.db,
        ticker="KXMLBGAME-26SEP271505HOUATH-HOU",
        game_id=824948,
        yes_team_id=117,
        outcomes_as_of=at(23, 30),
        source_ceiling=ceiling,
        mode="synthetic",
    )
    assert joined.status == "binary_scorable"
    decided = outcomes.join(
        store.db,
        ticker="KXMLBGAME-26SEP271505HOUATH-HOU",
        game_id=824948,
        yes_team_id=117,
        outcomes_as_of=at(23, 30),
        source_ceiling=ceiling,
        mode="synthetic",
        scheduled_start=at(19, 5),
    )
    assert decided.status == "schedule_changed_after_decision" and not decided.scorable


@pytest.mark.parametrize(
    "game_date", ["2026-09-28T17:05:00 EDT", "TBD", "2026-09-28T17:05:00", "2026-09-28"]
)
def test_an_unreadable_mapped_start_is_a_disposition_not_an_exception(
    tmp_path, monkeypatch, game_date
):
    """Mapping policy 2 stores the provider's gameDate verbatim and, on the
    rescheduledFromDate path, never parses it (review F19): a start the evaluator cannot
    read as an aware instant cannot confirm the horizon, so it is reported, not raised."""
    config = Config(database=str(tmp_path / "v2.sqlite3"), min_free_bytes=1, eligibility_rules=V2)
    store = Store(config.database, min_free_bytes=1)
    try:
        run_slice(store, config)  # rule-v2 cohort: eligible decision for 19:05Z, published
        evidence = postponed(dated_settlement())
        evidence["schedule"]["dates"][0]["games"][0]["gameDate"] = game_date
        settle(store, config, monkeypatch, evidence)
        mapping = json.loads(
            store.db.execute(
                "SELECT data_json FROM settlement_versions WHERE kind='mapping'"
            ).fetchone()[0]
        )
        assert mapping["status"] == "mapped" and mapping["mapping_policy"] == 2
        assert mapping["scheduled_start"] == game_date
        clock = Clock()
        clock.origin = at(23, 45)
        result = evaluate(
            store,
            V2_PROTO,
            view="forward-shadow",
            outcomes_as_of=at(23, 30),
            clock=clock.now,
            monotonic=clock.mono,
        )
        report = result["report"]
        assert report["outcome_status"] == {"schedule_changed_after_decision": 1}
        assert report["outcome_reasons"] == {"mapping_start_unknown": 1}
        assert report["scores"]["paired"]["n"] == 0 and result["status"] == "inconclusive"
    finally:
        store.close()


def moved_handler(start):
    data = fixture_data()
    for entry in data["responses"]:
        if entry["stage"] == "schedule":
            body = json.loads(entry["body"])
            body["dates"][0]["games"][0]["gameDate"] = start
            entry["body"] = json.dumps(body)
    handler, _, _ = handler_for(data)
    return handler


def test_a_revision_reverted_before_cutoff_does_not_taint_the_decision(store, config):
    to_schema4(store)
    collect(store, config, origin=at(17, 58))
    assert tick_at(store, PROTO, at(17, 58, 30))["bindings_prepared"] == 1
    moved = collect(store, config, origin=at(18, 0), handler=moved_handler("2026-09-27T19:35:00Z"))
    assert moved["eligibility_reasons"] == {"schedule_rule_time_mismatch": 2}
    tick_at(store, PROTO, at(18, 0, 30))  # witnesses the revised pass
    restored = collect(store, config, origin=at(18, 2))
    assert restored["eligibility_reasons"] == {"eligible": 2}
    tick_at(store, PROTO, at(18, 3))  # witnesses the restored pass before C
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_eligible"] == 1 and due["publications"]["allowed"] == 1


def test_a_revision_still_in_force_at_cutoff_abstains_with_its_reason(store, config):
    to_schema4(store)
    collect(store, config, origin=at(17, 58))
    tick_at(store, PROTO, at(17, 58, 30))
    collect(store, config, origin=at(18, 2), handler=moved_handler("2026-09-27T19:35:00Z"))
    tick_at(store, PROTO, at(18, 3))
    due = tick_at(store, PROTO, at(18, 5, 30))
    assert due["decisions_eligible"] == 0 and due["decisions_abstained"] == 1
    (payload,) = [
        json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
    ]
    assert "not_pregame" in payload["reasons"]
    assert "schedule_rule_time_mismatch" in payload["reasons"]
    assert payload["results"]["midpoint-v1"]["probability"] is None
