"""Rule v2 through the real collection → witness → tick → settlement → evaluation path.

Synthetic throughout (the shipped fixture rewritten to the date-only postseason wording,
injected transport and clocks). Proves that rule v2 is opt-in, forms its own collector
cohort, decides at C from evidence available at C, labels outcomes through mapping
policy 2, and that settlement replay derives the same mapping from the recorded rules.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from _f34 import at, collect, fixture_data, protocol, tick_at, to_schema4
from conftest import Clock
from test_collector import handler_for
from test_eligibility_rule_v2 import DATED, V2, dated
from test_materialization import settle, settlement_evidence

from vader_intelligence.config import Config
from vader_intelligence.evaluation.service import evaluate
from vader_intelligence.forecast.references import digest
from vader_intelligence.storage import Store

PROTO = protocol(dataset="rule-v2")


@pytest.fixture
def v2_config(tmp_path):
    return Config(database=str(tmp_path / "v2.sqlite3"), min_free_bytes=1, eligibility_rules=V2)


def dated_handler():
    handler, _, _ = handler_for(dated(fixture_data()))
    return handler


def run_slice(store, config):
    to_schema4(store)  # schema 5 / record contract 3
    collected = collect(store, config, origin=at(18, 3), handler=dated_handler())
    warm = tick_at(store, PROTO, at(18, 3, 30))
    due = tick_at(store, PROTO, at(18, 5, 30))
    return collected, warm, due


def test_default_configuration_finds_no_eligible_contract(config):
    store = Store(config.database, min_free_bytes=1)
    try:
        collected, warm, due = run_slice(store, config)
        assert collected["eligibility_reasons"] == {"unrecognized_full_game_rule": 2}
        assert warm["bindings_prepared"] == 0 and due["decisions_eligible"] == 0
    finally:
        store.close()


def test_rule_v2_decides_and_publishes_from_evidence_available_at_cutoff(v2_config):
    store = Store(v2_config.database, min_free_bytes=1)
    try:
        collected, warm, due = run_slice(store, v2_config)
        assert collected["eligibility_reasons"] == {"eligible": 2} and collected["books"] == 2
        assert warm["bindings_prepared"] == 1
        assert due["decisions_eligible"] == 1 and due["publications"]["allowed"] == 1
        db = store.db
        (payload,) = [
            json.loads(r[0]) for r in db.execute("SELECT payload_json FROM forecast_decisions")
        ]
        assert payload["contract_version"] == 3 and payload["cutoff_eligible"] is True
        assert payload["scheduled_start"] == "2026-09-27T19:05:00+00:00"
        assert payload["cutoff"] == "2026-09-27T18:05:00+00:00"
        assert payload["results"]["midpoint-v1"]["probability"] == "0.6450"
        assert payload["manifest"]["renewal"]["pregame"] is True
        # The pass records the rule set, and the collector cohort is distinct from a
        # default-configuration cohort (the pinned hash differs).
        pass_payload = json.loads(
            db.execute("SELECT payload_json FROM discovery_passes ORDER BY id DESC").fetchone()[0]
        )
        assert pass_payload["manifest"]["policy"]["eligibility_rules"] == list(V2)
        assert pass_payload["manifest"]["policy"]["mapping_version"] == 2
        default = Config(database=v2_config.database, min_free_bytes=1)
        pinned = payload["manifest"]["discovery"]["collector_config_hash"]
        assert pinned == digest(v2_config.provenance())
        assert pinned != digest(default.provenance())
        rows = db.execute(
            "SELECT json_extract(data_json,'$.mapping_version') FROM eligibility WHERE eligible=1"
        ).fetchall()
        assert {r[0] for r in rows} == {2}
    finally:
        store.close()


def dated_settlement(**kwargs):
    evidence = settlement_evidence(**kwargs)
    evidence["market"]["rules_primary"] = DATED.format(winner=evidence["market"]["yes_sub_title"])
    return evidence


def test_rule_v2_outcome_is_labelled_through_mapping_policy_two(v2_config, monkeypatch):
    store = Store(v2_config.database, min_free_bytes=1)
    try:
        run_slice(store, v2_config)
        settle(store, v2_config, monkeypatch, dated_settlement())
        mapping = json.loads(
            store.db.execute(
                "SELECT data_json FROM settlement_versions WHERE kind='mapping'"
            ).fetchone()[0]
        )
        assert mapping["status"] == "mapped" and mapping["mapping_policy"] == 2
        assert mapping["game_id"] == 824948
        clock = Clock()
        clock.origin = at(23, 45)
        result = evaluate(
            store,
            PROTO,
            view="forward-shadow",
            outcomes_as_of=at(23, 30),
            clock=clock.now,
            monotonic=clock.mono,
        )
        paired = result["report"]["scores"]["paired"]
        assert result["status"] == "complete" and paired["n"] == 1
        assert result["report"]["record_contract"] == 3
    finally:
        store.close()


def test_a_default_settlement_run_still_quarantines_the_date_only_rule(config, monkeypatch):
    store = Store(config.database, min_free_bytes=1)
    try:
        to_schema4(store)
        settle(store, config, monkeypatch, dated_settlement())
        mapping = json.loads(
            store.db.execute(
                "SELECT data_json FROM settlement_versions WHERE kind='mapping'"
            ).fetchone()[0]
        )
        assert mapping["status"] == "quarantined"
        assert mapping["reason"] == "unrecognized_full_game_rule"
    finally:
        store.close()


def test_settlement_replay_derives_the_same_mapping_from_the_recorded_rules(v2_config, monkeypatch):
    from vader_intelligence.replay import replay as settlement_replay

    store = Store(v2_config.database, min_free_bytes=1)
    try:
        to_schema4(store)
        settle(store, v2_config, monkeypatch, dated_settlement())
        before = store.db.execute(
            "SELECT id, data_json FROM settlement_versions ORDER BY id"
        ).fetchall()
        # Replay runs without any configuration of its own: the rules come from the
        # original settlement run's provenance, so nothing is re-mapped differently.
        settlement_replay(store)
        after = store.db.execute(
            "SELECT id, data_json FROM settlement_versions ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
    finally:
        store.close()


def test_start_revised_after_binding_abstains_at_cutoff_with_the_rule_v2_reason(v2_config):
    """A later pass before C sees the official start moved away from the ticker time:
    renewal at C fails pregame with the v2 reason, never falling back to the binding."""
    store = Store(v2_config.database, min_free_bytes=1)
    try:
        to_schema4(store)
        collect(store, v2_config, origin=at(18, 0), handler=dated_handler())
        assert tick_at(store, PROTO, at(18, 0, 30))["bindings_prepared"] == 1
        moved = dated(fixture_data())
        for entry in moved["responses"]:
            if entry["stage"] == "schedule":
                body = json.loads(entry["body"])
                body["dates"][0]["games"][0]["gameDate"] = "2026-09-27T19:35:00Z"
                entry["body"] = json.dumps(body)
        handler, _, _ = handler_for(moved)
        later = collect(store, v2_config, origin=at(18, 3), handler=handler)
        assert later["eligibility_reasons"] == {"ticker_start_mismatch": 2}
        # Unwitnessed by C, the later pass would not apply (the earlier pass stays the
        # applicable one); the shadow cycle's tick witnesses it before C.
        tick_at(store, PROTO, at(18, 3, 30))
        due = tick_at(store, PROTO, at(18, 5, 30))
        assert due["decisions_eligible"] == 0 and due["decisions_abstained"] == 1
        (payload,) = [
            json.loads(r[0])
            for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
        ]
        assert payload["cutoff_eligible"] is False
        assert "not_pregame" in payload["reasons"]
        assert "ticker_start_mismatch" in payload["reasons"]
        assert payload["manifest"]["renewal"]["pregame"] is False
        assert payload["manifest"]["renewal"]["identity"] is True
    finally:
        store.close()
