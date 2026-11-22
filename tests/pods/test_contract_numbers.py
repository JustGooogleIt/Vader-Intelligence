"""Untrusted numbers in operation contracts and provider grids (reviews F42, F43, F45).

Every malformed value is a recorded rejection (ContractError), never another exception
and never a task that can only fail."""

import pytest

from vader_intelligence.pods import planner
from vader_intelligence.pods.contracts import ContractError
from vader_intelligence.pods.operations import normalized_params

HUGE = 10**400  # valid JSON, finite as an integer, too large for a float


def params(**hyperparameters):
    return {
        "dataset_manifest_hash": "a" * 64,
        "feature_set": "fs-market-v1",
        "feature_set_version": "v",
        "model_family": "logistic",
        "seed": 0,
        "hyperparameters": {"grid": {"C": [0.1, 1.0]}, "n_folds": 3, **hyperparameters},
    }


def test_a_huge_grid_integer_is_a_contract_error():  # F45
    with pytest.raises(ContractError):
        normalized_params("train_model", params(grid={"C": [HUGE]}))


@pytest.mark.parametrize(
    "name, value",
    [
        ("n_folds", 2.5),
        ("max_configs", 1.5),
        ("n_folds", True),
        ("max_configs", False),
        ("search_seconds", True),
        ("search_seconds", HUGE),
    ],
)
def test_counts_must_be_integers_and_seconds_a_real_number(name, value):  # F43
    with pytest.raises(ContractError):
        normalized_params("train_model", params(**{name: value}))


def test_a_provider_grid_with_a_huge_integer_is_a_recorded_rejection():  # F42
    snapshot = {"sha256": "a" * 64, "feature_set": "fs-market-v1", "feature_set_version": "v"}
    ctx = {"operations": ("train_model",), "role": "training", "snapshot": snapshot}
    proposal = planner.Proposal("train_model", params(grid={"C": [HUGE]}))
    with pytest.raises(ContractError):
        planner.validate(ctx, proposal)
    with pytest.raises(ContractError):
        planner._check_grid(
            {"model_family": "logistic", "hyperparameters": {"grid": {"C": [HUGE]}}}
        )


def test_valid_numbers_still_pass():
    assert normalized_params("train_model", params(max_configs=8, search_seconds=30))
