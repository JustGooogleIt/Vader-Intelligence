"""Symbolic search integration (roadmap C) on a SYNTHETIC registry dataset."""

import hashlib
import json

import numpy as np
import pytest

from datasets.test_training import SPLIT, synthetic_dataset
from vader_intelligence.cli import execute, parser
from vader_intelligence.features import definitions
from vader_intelligence.forecast.forecasters import (
    ForecasterRunner,
    predict_artifact,
    verify_artifact,
)
from vader_intelligence.models.artifact import ModelArtifact, predict_proba
from vader_intelligence.research import symbolic_search, training
from vader_intelligence.research.registry import Registry
from vader_intelligence.symbolic import SearchConfig

SMALL = SearchConfig(population=60, generations=8, max_candidates=3000, max_seconds=60.0, seed=1)


@pytest.fixture
def registry(tmp_path):
    r = Registry(tmp_path / "research")
    try:
        yield r
    finally:
        r.close()


def test_search_registers_a_replayable_artifact_and_the_full_history(registry):
    manifest, rows = synthetic_dataset()
    dataset_id = registry.register_dataset(manifest, rows)
    result = symbolic_search.run(registry, dataset_id, split=SPLIT, config=SMALL)
    assert result["status"] == "complete" and result["validation"]["selection_biased"] is True
    record = symbolic_search.search_record(registry, result["experiment_id"])
    assert record["validation_metrics_selection_biased"] is True
    assert record["counters"]["candidates_evaluated"] > 0 and record["history"]
    assert record["front"] and record["selected"]["description"] == result["expression"]
    artifact = ModelArtifact.from_dict(json.loads(registry.model_artifact(result["model_id"])))
    assert artifact.family == "symbolic"
    assert artifact.training["candidates_evaluated"] == record["counters"]["candidates_evaluated"]
    assert artifact.training["search_stop_reason"] == record["stop_reason"]
    # The holdout was never touched by the search.
    assert result["holdout"]["untouched"] is True
    # The planted relation is in the market mid: the selected expression should use it.
    assert "mid" in result["expression"]


def test_symbolic_artifacts_share_the_holdout_ledger_with_other_models(registry):
    manifest, rows = synthetic_dataset()
    dataset_id = registry.register_dataset(manifest, rows)
    symbolic = symbolic_search.run(registry, dataset_id, split=SPLIT, config=SMALL)
    logistic = training.train(registry, dataset_id, family="logistic", split=SPLIT)
    first = training.evaluate_holdout(registry, dataset_id, symbolic["model_id"], SPLIT)
    second = training.evaluate_holdout(registry, dataset_id, logistic["model_id"], SPLIT)
    assert first["ledger"]["evaluations"] == 1 and second["ledger"]["evaluations"] == 2
    assert second["ledger"]["untouched"] is False


def test_a_symbolic_artifact_can_be_a_forward_forecaster(registry):
    manifest, rows = synthetic_dataset()
    dataset_id = registry.register_dataset(manifest, rows)
    result = symbolic_search.run(registry, dataset_id, split=SPLIT, config=SMALL)
    data = registry.model_artifact(result["model_id"])
    # The registry path verifies artifacts (parse + zero-row inference): a real symbolic
    # artifact passes and keeps its digest; its params are validated at construction.
    runner = ForecasterRunner({result["model_id"]: data}, predict_artifact, verify=verify_artifact)
    assert runner.models[result["model_id"]]["error"] is None
    (descriptor,) = runner.descriptors()
    assert descriptor["family"] == "symbolic" and descriptor["feature_set"] == "fs-market-v1"
    names = [d.name for d in definitions.feature_set("fs-market-v1")]
    values = [rows[0]["features"][n] for n in names]
    artifact = ModelArtifact.from_dict(json.loads(data))
    assert artifact.sha256 == result["model_id"] and artifact.to_json().encode() == data
    tampered = json.loads(data)
    tampered["params"]["model"]["feature_names"].reverse()
    tampered = json.dumps(tampered, sort_keys=True).encode()
    sha = hashlib.sha256(tampered).hexdigest()
    refused = ForecasterRunner({sha: tampered}, predict_artifact, verify=verify_artifact)
    assert "invalid symbolic artifact" in refused.models[sha]["error"]
    expected = predict_proba(
        artifact, np.array([[np.nan if v is None else v for v in values]], dtype=float)
    )[0]
    assert predict_artifact(data, values) == pytest.approx(expected, abs=0)


def test_cli_symbolic_search(tmp_path):
    root = tmp_path / "research"
    registry = Registry(root)
    manifest, rows = synthetic_dataset()
    dataset_id = registry.register_dataset(manifest, rows)
    registry.close()
    result = execute(
        parser().parse_args(
            [
                "symbolic",
                "search",
                "--registry",
                str(root),
                "--dataset",
                dataset_id,
                "--development-through",
                SPLIT.development_through,
                "--validation-through",
                SPLIT.validation_through,
                "--population",
                "40",
                "--generations",
                "4",
                "--max-candidates",
                "1000",
                "--seed",
                "2",
            ]
        )
    )
    assert result["status"] == "complete" and result["search"]["counters"]["candidates_evaluated"]
