"""Registered forecasters at C: decision contract 4 and evaluation contract 2 (D-004).

Synthetic throughout. A stub artifact (prediction = the forward mid_c feature) isolates
the forward path from any model family: features come only from listing quotes retrieved
and witnessed by C, the decision records the model result beside the baseline pair, and
the evaluator scores it on the same population.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from _f34 import at, collect, protocol, tick_at
from test_materialization import settle, settlement_evidence

from vader_intelligence.evaluation.service import evaluate
from vader_intelligence.features import definitions
from vader_intelligence.forecast import journal, schema
from vader_intelligence.forecast.forecasters import ForecasterRunner, witnessed_listing
from vader_intelligence.forecast.research import Protocol
from vader_intelligence.research import report as reports

NAMES = [d.name for d in definitions.feature_set("fs-market-v1")]


def stub_artifact(feature_set="fs-market-v1", names=None):
    data = json.dumps(
        {
            "family": "stub",
            "feature_names": names or [d.name for d in definitions.feature_set(feature_set)],
            "training": {"feature_set": feature_set},
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(data).hexdigest(), data


def predict_mid(data, values):
    if values[0] is None:
        raise ValueError("mid_c missing")
    return values[0]


def to_schema6(store):
    from vader_intelligence.settlement.schema import migrate as settlement_migrate

    settlement_migrate(store)
    schema.migrate(store.db)
    assert schema.migrate(store.db, record_contract=4) is True
    assert store.schema_version() == 6


def runner_and_protocol(dataset="models"):
    sha, data = stub_artifact()
    return ForecasterRunner({sha: data}, predict_mid), Protocol(dataset=dataset, forecasters=[sha])


def test_contract_four_decision_carries_the_forward_model_result(store, config, monkeypatch):
    to_schema6(store)
    runner, proto = runner_and_protocol()
    collect(store, config, origin=at(18, 3))
    tick_at_runner(store, proto, at(18, 3, 30), runner)
    due = tick_at_runner(store, proto, at(18, 5, 30), runner)
    assert due["decisions_eligible"] == 1 and due["publications"]["allowed"] == 1
    (payload,) = [
        json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
    ]
    assert payload["contract_version"] == 4
    (forecaster,) = payload["forecasters"]
    model = payload["results"][forecaster["id"]]
    assert (
        model["reasons"] == []
        and model["inputs"]["artifact_sha256"] == forecaster["artifact_sha256"]
    )
    assert model["probability"] == f"{model['inputs']['features']['mid_c']:.6f}"
    assert model["inputs"]["selected_quotes"] >= 1
    assert journal.integrity(store.db)["record_contracts"]["forecast_decisions"]["4"] == 1

    settle(store, config, monkeypatch, settlement_evidence())
    from conftest import Clock

    clock = Clock()
    clock.origin = at(23, 45)
    result = evaluate(
        store,
        proto,
        view="forward-shadow",
        outcomes_as_of=at(23, 30),
        clock=clock.now,
        monotonic=clock.mono,
    )
    assert result["status"] == "complete"
    summary = result["report"]["forecasters"][forecaster["id"]]
    assert summary["n"] == 1 and summary["paired_with_midpoint"]["n"] == 1
    row = json.loads(store.db.execute("SELECT payload_json FROM evaluation_runs").fetchone()[0])
    assert row["contract_version"] == 2 and row["forecasters"] == payload["forecasters"]
    item = json.loads(store.db.execute("SELECT payload_json FROM evaluation_items").fetchone()[0])
    assert set(item["scores"]) == {"constant-v1", "midpoint-v1", forecaster["id"]}
    assert item["scores"][forecaster["id"]] is not None
    text = reports.text_report(store.db, result["evaluation_id"])
    assert f"Forecaster {forecaster['id']} (stub, fs-market-v1) n=1" in text
    assert journal.integrity(store.db)["integrity"] == "verified"


def tick_at_runner(store, proto, moment, runner):
    from conftest import Clock

    from vader_intelligence.forecast.tick import tick

    clock = Clock()
    clock.origin = moment
    return tick(store, proto, clock=clock.now, monotonic=clock.mono, runner=runner)


def test_only_quotes_witnessed_by_the_cutoff_feed_a_model(store, config):
    to_schema6(store)
    collect(store, config, origin=at(18, 3))
    cutoff = at(18, 5)
    unwitnessed = witnessed_listing(
        store.db, "KXMLBGAME-26SEP271505HOUATH-HOU", cutoff, "local-witness-v1", "synthetic"
    )
    assert unwitnessed.quotes == ()  # retrieved by C but not yet witnessed
    runner, proto = runner_and_protocol()
    tick_at_runner(store, proto, at(18, 3, 30), runner)  # witnesses the collection
    witnessed = witnessed_listing(
        store.db, "KXMLBGAME-26SEP271505HOUATH-HOU", cutoff, "local-witness-v1", "synthetic"
    )
    assert witnessed.quotes and all(q.at <= cutoff for q in witnessed.quotes)


def restamp_fetches(db, hours):
    """Rewrite every retrieval time as the same instant at another UTC offset."""
    from datetime import datetime, timedelta, timezone

    zone = timezone(timedelta(hours=hours))
    for fetch_id, stamp in db.execute(
        "SELECT id, retrieved_at FROM fetches WHERE retrieved_at IS NOT NULL"
    ).fetchall():
        moved = datetime.fromisoformat(stamp).astimezone(zone).isoformat()
        db.execute("UPDATE fetches SET retrieved_at=? WHERE id=?", (moved, fetch_id))
    db.commit()


@pytest.mark.parametrize("hours", [2, 14])
def test_witnessed_listing_compares_retrieval_instants(store, config, hours):
    """Review F27: the C and C - LOOKBACK bounds compare instants, not offset text. A
    retrieval at 18:03Z stamped 20:03+02:00 is inside the window; one at 15:00Z (before
    C - 2 h = 16:05Z) stamped 17:00+02:00 is outside it."""
    to_schema6(store)
    runner, proto = runner_and_protocol()
    collect(store, config, origin=at(15, 0))
    tick_at_runner(store, proto, at(15, 0, 30), runner)  # witnessed, but too old
    collect(store, config, origin=at(18, 3))
    tick_at_runner(store, proto, at(18, 3, 30), runner)
    cutoff, ticker = at(18, 5), "KXMLBGAME-26SEP271505HOUATH-HOU"
    utc = witnessed_listing(store.db, ticker, cutoff, "local-witness-v1", "synthetic")
    assert utc.quotes and all(at(16, 5) <= q.at <= cutoff for q in utc.quotes)
    restamp_fetches(store.db, hours)
    moved = witnessed_listing(store.db, ticker, cutoff, "local-witness-v1", "synthetic")
    assert [q.ref for q in moved.quotes] == [q.ref for q in utc.quotes]
    assert [q.at for q in moved.quotes] == [q.at for q in utc.quotes]


def test_research_only_feature_sets_cannot_run_forward():
    sha, data = stub_artifact("fs-full-v1")
    with pytest.raises(ValueError, match="not forward capable"):
        ForecasterRunner({sha: data}, predict_mid)
    sha, data = stub_artifact(names=list(reversed(NAMES)))
    with pytest.raises(ValueError, match="features differ"):
        ForecasterRunner({sha: data}, predict_mid)
    with pytest.raises(ValueError, match="verification"):
        ForecasterRunner({"0" * 64: data}, predict_mid)


@pytest.mark.parametrize(
    "document",
    [
        [1, 2],
        "artifact",
        None,
        {"training": ["fs-market-v1"]},
        {"training": "fs-market-v1", "feature_names": NAMES},
        {"training": {"feature_set": ["fs-market-v1"]}, "feature_names": NAMES},
    ],
)
def test_a_malformed_artifact_document_is_a_configuration_error(document):
    """Port N-33b regression from Claude's local 0572248; preserve clean CLI errors."""
    data = json.dumps(document).encode()
    sha = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match=sha[:12]):
        ForecasterRunner({sha: data}, predict_mid)


def test_forecasters_need_schema_six_and_their_artifacts(store, config):
    from _f34 import to_schema4

    to_schema4(store)  # schema 5
    runner, proto = runner_and_protocol()
    with pytest.raises(ValueError, match="record-contract 4"):
        tick_at_runner(store, proto, at(18, 3, 30), runner)
    schema.migrate(store.db, record_contract=4)
    with pytest.raises(ValueError, match="model artifacts"):
        tick_at_runner(store, proto, at(18, 3, 30), None)
    other_sha, other = stub_artifact(names=None)
    mismatched = Protocol(dataset="models", forecasters=["f" * 64])
    with pytest.raises(ValueError, match="differ from the protocol"):
        tick_at_runner(store, mismatched, at(18, 3, 30), runner)
    assert store.db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0] == 0
    del other_sha, other


def test_schema_six_closes_older_contracts_for_new_rows(store, config):
    to_schema6(store)
    runner, proto = runner_and_protocol()
    collect(store, config, origin=at(18, 3))
    tick_at_runner(store, proto, at(18, 3, 30), runner)
    tick_at_runner(store, proto, at(18, 5, 30), runner)
    payload = json.loads(
        store.db.execute("SELECT payload_json FROM forecast_decisions").fetchone()[0]
    )
    older = dict(payload, contract_version=3, dataset="older")
    del older["forecasters"]
    older["results"] = {k: v for k, v in payload["results"].items() if not k.startswith("model:")}
    with pytest.raises(ValueError, match="closed for new forecast_decisions"):
        journal.append(store.db, "forecast_decisions", older)
    lying = dict(payload, dataset="lying")
    lying["results"] = dict(payload["results"])
    lying["results"].pop(payload["forecasters"][0]["id"])
    with pytest.raises(ValueError, match="declared forecasters"):
        journal.append(store.db, "forecast_decisions", lying)


def test_a_protocol_without_forecasters_keeps_its_hash_and_writes_contract_four(store, config):
    assert Protocol(dataset="demo-forward").config_hash == (
        "9664f12169b15965b386f6a475a7c728c3901e248bdaac9fabe9c910777c81cf"
    )
    to_schema6(store)
    proto = protocol("plain")
    collect(store, config, origin=at(18, 3))
    tick_at(store, proto, at(18, 3, 30))
    tick_at(store, proto, at(18, 5, 30))
    payload = json.loads(
        store.db.execute("SELECT payload_json FROM forecast_decisions").fetchone()[0]
    )
    assert payload["contract_version"] == 4 and payload["forecasters"] == []
    assert set(payload["results"]) == {"constant-v1", "midpoint-v1"}


def test_a_trained_artifact_runs_at_the_cutoff_through_the_cli(tmp_path, config):
    """Train a real logistic artifact on a synthetic registry dataset, register it as a
    forecaster, and let the CLI tick compute its forecast from witnessed evidence."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "datasets"))
    from test_training import SPLIT, synthetic_dataset

    from vader_intelligence.cli import execute, parser
    from vader_intelligence.forecast.forecasters import predict_artifact
    from vader_intelligence.research import training
    from vader_intelligence.research.registry import Registry
    from vader_intelligence.storage import Store

    root = tmp_path / "research"
    registry = Registry(root)
    manifest, rows = synthetic_dataset()
    dataset_id = registry.register_dataset(manifest, rows)
    model_id = training.train(registry, dataset_id, family="logistic", split=SPLIT)["model_id"]
    artifact = registry.model_artifact(model_id)
    registry.close()

    db_path = Path(config.database)
    store = Store(db_path, min_free_bytes=1)
    to_schema6(store)
    collect(store, config, origin=at(18, 3))
    proto = Protocol(dataset="trained", forecasters=[model_id])
    runner = ForecasterRunner({model_id: artifact}, predict_artifact)
    tick_at_runner(store, proto, at(18, 3, 30), runner)
    tick_at_runner(store, proto, at(18, 5, 30), runner)
    (payload,) = [
        json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
    ]
    store.close()
    result = payload["results"]["model:" + model_id[:16]]
    names = [d.name for d in definitions.feature_set("fs-market-v1")]
    expected = predict_artifact(artifact, [result["inputs"]["features"][n] for n in names])
    assert result["probability"] == f"{min(max(expected, 1e-6), 1 - 1e-6):.6f}"

    research_toml = tmp_path / "research.toml"
    research_toml.write_text(f'[research]\ndataset = "cli"\nforecasters = ["{model_id}"]\n')
    with pytest.raises(ValueError, match="--model-registry"):
        execute(
            parser().parse_args(
                ["--db", str(db_path), "forecast", "tick", "--research-config", str(research_toml)]
            )
        )
    ran = execute(
        parser().parse_args(
            [
                "--db",
                str(db_path),
                "forecast",
                "tick",
                "--research-config",
                str(research_toml),
                "--model-registry",
                str(root),
            ]
        )
    )
    assert ran["status"] in ("complete", "inconclusive", "partial")


def test_an_unloadable_or_missing_artifact_abstains_and_the_tick_still_decides(store, config):
    from vader_intelligence.forecast.forecasters import verify_artifact

    to_schema6(store)
    sha, data = stub_artifact()  # digest and feature checks pass; not a loadable model
    missing = "f" * 64
    runner = ForecasterRunner({sha: data, missing: None}, predict_mid, verify=verify_artifact)
    proto = Protocol(dataset="models", forecasters=sorted([sha, missing]))
    collect(store, config, origin=at(18, 3))
    tick_at_runner(store, proto, at(18, 3, 30), runner)
    due = tick_at_runner(store, proto, at(18, 5, 30), runner)
    assert due["decisions_eligible"] == 1 and not due["errors"]  # the baseline decision
    (payload,) = [
        json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
    ]
    for entry in payload["forecasters"]:
        result = payload["results"][entry["id"]]
        assert result["probability"] is None
        assert result["reasons"][0].startswith("model_unavailable:")


@pytest.mark.parametrize("state", ["missing", "uninitialized"])
def test_unavailable_registry_does_not_suppress_baselines(store, config, tmp_path, state):
    from vader_intelligence.forecast.forecasters import runner_from_registry

    to_schema6(store)
    root = tmp_path / "unavailable-registry"
    if state == "uninitialized":
        root.mkdir()
    proto = Protocol(dataset="registry-unavailable", forecasters=["f" * 64])
    runner = runner_from_registry(root, proto)
    collect(store, config, origin=at(18, 3))
    tick_at_runner(store, proto, at(18, 3, 30), runner)
    due = tick_at_runner(store, proto, at(18, 5, 30), runner)
    assert due["decisions_eligible"] == 1 and not due["errors"]
    payload = json.loads(
        store.db.execute("SELECT payload_json FROM forecast_decisions").fetchone()[0]
    )
    assert payload["results"]["constant-v1"]["probability"] == "0.5"
    result = payload["results"][payload["forecasters"][0]["id"]]
    assert result["probability"] is None
    assert result["reasons"][0].startswith("model_unavailable:")
    assert not (root / "research.sqlite3").exists()


def fixed_output_artifact(output):
    """A stub artifact whose prediction is `output` (a string, decoded by predict_output)."""
    data = json.dumps(
        {
            "family": "stub",
            "feature_names": NAMES,
            "output": output,
            "training": {"feature_set": "fs-market-v1"},
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(data).hexdigest(), data


def predict_output(data, values):
    output = json.loads(data)["output"]
    special = {"none": None, "text": "0.5", "true": True}
    return special[output] if output in special else float(output)


VALID_OUTPUTS = {"0.0": "0.000001", "1.0": "0.999999", "0.25": "0.250000", "0.9999996": "0.999999"}


@pytest.mark.parametrize(
    "invalid",
    [["nan", "inf", "-inf", "1.7", "-0.2", "1.0000001"], ["none", "text", "true", "-0.0000001"]],
)
def test_an_invalid_model_output_abstains_and_the_decision_is_still_recorded(
    store, config, invalid
):
    """NaN, infinite, out-of-range or non-numeric model outputs abstain (never clipped
    into a forecast, never failing the decision); [0, 1] keeps the six-place edge clip."""
    to_schema6(store)
    artifacts = dict(fixed_output_artifact(o) for o in invalid + list(VALID_OUTPUTS))
    output_of = {sha: json.loads(data)["output"] for sha, data in artifacts.items()}
    runner = ForecasterRunner(artifacts, predict_output)
    proto = Protocol(dataset="models", forecasters=sorted(artifacts))
    collect(store, config, origin=at(18, 3))
    tick_at_runner(store, proto, at(18, 3, 30), runner)
    due = tick_at_runner(store, proto, at(18, 5, 30), runner)
    assert due["decisions_eligible"] == 1 and not due["errors"]
    (payload,) = [
        json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
    ]
    assert payload["results"]["constant-v1"]["probability"] == "0.5"
    assert payload["results"]["midpoint-v1"]["probability"] is not None
    reasons = {}
    for entry in payload["forecasters"]:
        result = payload["results"][entry["id"]]
        output = output_of[entry["artifact_sha256"]]
        reasons[output] = result["reasons"]
        if output in VALID_OUTPUTS:
            assert result["probability"] == VALID_OUTPUTS[output] and result["reasons"] == []
        else:
            assert result["probability"] is None and result["inputs"] == {}, output
            (reason,) = result["reasons"]
            assert reason.startswith("model_output_invalid:"), (output, reason)
    expected = {"nan": "nan", "1.7": "1.7", "-inf": "-inf", "none": "NoneType", "text": "str"}
    for output, detail in expected.items():
        if output in invalid:
            assert reasons[output] == ["model_output_invalid:" + detail]
    assert journal.integrity(store.db)["integrity"] == "verified"
