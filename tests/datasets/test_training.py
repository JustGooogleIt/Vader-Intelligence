"""Training lifecycle on a SYNTHETIC registry dataset (roadmap B3).

The planted relation (the outcome follows a sharpened market mid plus noise) only checks
that the pipeline can learn and is wired correctly; it is not evidence about markets.
"""

import hashlib
import json
from datetime import date, timedelta

import numpy as np
import pytest

from vader_intelligence.cli import execute, parser
from vader_intelligence.datasets.splits import SplitDefinition
from vader_intelligence.features import definitions
from vader_intelligence.provenance import json_text
from vader_intelligence.research import training
from vader_intelligence.research.registry import Registry

SPLIT = SplitDefinition("2026-07-31", "2026-08-31", embargo_days=1)


def synthetic_dataset(n_days=200, per_day=4, seed=7, flip_after=None):
    rng = np.random.default_rng(seed)
    names = [d.name for d in definitions.feature_set("fs-market-v1")]
    rows, start, game = [], date(2026, 4, 1), 900000
    for d in range(n_days):
        day = (start + timedelta(days=d)).isoformat()
        for _ in range(per_day):
            game += 1
            mid = float(np.clip(rng.beta(4, 4), 0.05, 0.95))
            truth = 1 / (1 + np.exp(-1.6 * np.log(mid / (1 - mid))))
            y = int(rng.random() < truth)
            if flip_after and day > flip_after:
                y = 1 - y  # used to prove later labels cannot change the fitted artifact
            features = {n: None for n in names}
            features.update(
                mid_c=mid,
                logit_mid_c=float(np.log(mid / (1 - mid))),
                spread_c=0.01,
                pair_mid_c=mid if rng.random() > 0.1 else None,
                selected_is_home=float(rng.random() < 0.5),
                postseason=0.0,
            )
            rows.append(
                {
                    "opportunity_key": f"game:{game}",
                    "group": game,
                    "official_date": day,
                    "start": day + "T23:00:00+00:00",
                    "cutoff": day + "T22:00:00+00:00",
                    "features": features,
                    "y": y,
                }
            )
    data = "".join(json_text(r) + "\n" for r in rows).encode()
    manifest = {
        "format": "dataset-v1",
        "name": f"synthetic-{seed}-{flip_after}",
        "evidence_kind": "synthetic",
        "feature_set": definitions.describe_set("fs-market-v1"),
        "rows": len(rows),
        "rows_sha256": hashlib.sha256(data).hexdigest(),
    }
    return manifest, rows


@pytest.fixture
def registry(tmp_path):
    r = Registry(tmp_path / "research")
    try:
        yield r
    finally:
        r.close()


def test_train_registers_a_deterministic_artifact_and_biased_validation(registry):
    manifest, rows = synthetic_dataset()
    dataset_id = registry.register_dataset(manifest, rows)
    first = training.train(registry, dataset_id, family="logistic", split=SPLIT, seed=3)
    again = training.train(registry, dataset_id, family="logistic", split=SPLIT, seed=3)
    assert first["model_id"] == again["model_id"]  # same data, config and seed
    validation = first["validation"]
    assert validation["selection_biased"] is True and validation["model"]["n"] > 50
    market = validation["baselines"]["market:mid_c"]
    assert market["coverage"] == validation["model"]["n"]
    assert set(market["model_minus_baseline_log_loss"]) == {"estimate", "ci95"}
    assert validation["baselines"]["market:pair_mid_c"]["coverage"] < market["coverage"]
    artifact = json.loads(registry.model_artifact(first["model_id"]))
    assert artifact["training"]["calibration_fit_on"] == "out-of-fold development predictions"
    assert artifact["training"]["split"]["key"] == SPLIT.key
    assert first["holdout"]["untouched"] is True  # training never touches the holdout


def test_labels_outside_development_cannot_change_the_artifact(registry):
    base_manifest, base_rows = synthetic_dataset()
    flipped_manifest, flipped_rows = synthetic_dataset(flip_after=SPLIT.development_through)
    a = registry.register_dataset(base_manifest, base_rows)
    b = registry.register_dataset(flipped_manifest, flipped_rows)
    model_a = json.loads(
        registry.model_artifact(training.train(registry, a, family="gbm", split=SPLIT)["model_id"])
    )
    model_b = json.loads(
        registry.model_artifact(training.train(registry, b, family="gbm", split=SPLIT)["model_id"])
    )
    for key in ("params", "preprocessor", "calibration"):
        assert model_a[key] == model_b[key], key


def test_holdout_is_consumed_once_and_then_reported_as_reused(registry):
    manifest, rows = synthetic_dataset()
    dataset_id = registry.register_dataset(manifest, rows)
    logistic = training.train(registry, dataset_id, family="logistic", split=SPLIT)
    gbm = training.train(registry, dataset_id, family="gbm", split=SPLIT, max_configs=2)
    first = training.evaluate_holdout(registry, dataset_id, logistic["model_id"], SPLIT)
    assert first["ledger"]["evaluations"] == 1 and first["holdout"]["model"]["n"] > 10
    second = training.evaluate_holdout(registry, dataset_id, gbm["model_id"], SPLIT)
    assert second["ledger"]["evaluations"] == 2 and second["ledger"]["untouched"] is False
    with pytest.raises(ValueError, match="already evaluated"):
        training.evaluate_holdout(registry, dataset_id, logistic["model_id"], SPLIT)
    other = SplitDefinition("2026-07-15", "2026-08-31", embargo_days=1)
    with pytest.raises(ValueError, match="split differs"):
        training.evaluate_holdout(registry, dataset_id, gbm["model_id"], other)


def test_insufficient_rows_are_refused(registry):
    manifest, rows = synthetic_dataset(n_days=20)
    dataset_id = registry.register_dataset(manifest, rows)
    with pytest.raises(ValueError, match="insufficient"):
        training.train(registry, dataset_id, family="logistic", split=SPLIT)


def test_cli_train_inspect_and_holdout(tmp_path):
    root = tmp_path / "research"
    registry = Registry(root)
    manifest, rows = synthetic_dataset()
    dataset_id = registry.register_dataset(manifest, rows)
    registry.close()

    def cli(*args):
        return execute(parser().parse_args(list(args)))

    common = [
        "--registry",
        str(root),
        "--dataset",
        dataset_id,
        "--development-through",
        SPLIT.development_through,
        "--validation-through",
        SPLIT.validation_through,
    ]
    trained = cli("model", "train", *common, "--family", "logistic", "--max-configs", "2")
    assert trained["status"] == "complete"
    listed = cli("model", "inspect", "--registry", str(root))
    assert [m["id"] for m in listed["models"]] == [trained["model_id"]]
    detail = cli("model", "inspect", "--registry", str(root), "--id", trained["model_id"])
    assert detail["evaluations"][0]["selection_biased"] is True
    held = cli("model", "holdout", *common, "--model", trained["model_id"])
    assert held["ledger"]["evaluations"] == 1
