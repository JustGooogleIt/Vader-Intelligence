"""Contracts, operation registry, artifact store and provider doubles.

No worker processes, no store and no network.
"""

import json
import os
import socket

import pytest

from vader_intelligence.pods import operations, providers
from vader_intelligence.pods.artifacts import ArtifactCorrupted, ArtifactStore
from vader_intelligence.pods.contracts import (
    RESULT_SCHEMA,
    ArtifactRef,
    Budgets,
    ContractError,
    ExperimentManifest,
    PodSpec,
    ResultManifest,
    WorkerInfo,
    parse_result_manifest,
    task_identity,
)
from vader_intelligence.pods.operations import (
    DeterministicFailure,
    UnknownOperation,
    get_operation,
    normalized_params,
    register_operation,
)

TASK = "0123456789abcdef0123456789abcdef"
TOKEN = "f" * 32


def manifest(**overrides):
    values = dict(
        schema=RESULT_SCHEMA,
        task_id=TASK,
        attempt_id=TASK + "-1",
        lease_token=TOKEN,
        status="succeeded",
        output={"value": 1},
        error=None,
        artifacts=(ArtifactRef("a.json", "test_output", "a" * 64, 3),),
        worker=WorkerInfo(pid=42, env_names=("HOME", "PATH"), rlimits={}, cpu_seconds=0.1),
    )
    values.update(overrides)
    return ResultManifest(**values)


def test_task_ids_are_deterministic_over_normalized_parameters():
    one = normalized_params("sleep", {"seconds": 1})
    same = normalized_params("sleep", {"seconds": 1.0, "with_child": False})
    assert one == same == {"seconds": 1.0, "with_child": False}
    assert task_identity("sleep", one, "a" * 64) == task_identity("sleep", same, "a" * 64)
    assert task_identity("sleep", one, "a" * 64) != task_identity("sleep", one, "b" * 64)
    assert len(task_identity("sleep", one, "a" * 64)) == 32


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"seconds": 1, "extra": 1},
        {"seconds": True},
        {"seconds": "1"},
        {"seconds": float("nan")},
        {"seconds": 61},
        {"seconds": 1, "with_child": 1},
    ],
)
def test_parameter_contracts_are_strict(params):
    with pytest.raises(ContractError):
        normalized_params("sleep", params)


def test_unknown_operation_and_role_contracts():
    with pytest.raises(UnknownOperation):
        get_operation("shell")
    assert get_operation("noop").roles == ("diagnostics",)
    assert "diagnostics" not in get_operation("train_model").roles
    with pytest.raises(ContractError):
        PodSpec("diag", "live_trading")
    with pytest.raises(ContractError):
        Budgets(api_cost_usd=0.01)


def test_experiment_manifest_never_names_a_holdout():
    good = dict(
        dataset_manifest_hash="0" * 64,
        feature_set_versions={},
        split={"development": {}, "validation": {}},
        code_hash="1" * 64,
        operation="noop",
        params={},
        seed=1,
        budget={},
    )
    assert ExperimentManifest.from_json(ExperimentManifest(**good).to_bytes()).seed == 1
    for change in (
        {"split": {"holdout": {}}},
        {"split": {"test": {}}},
        {"split": {}},
        {"params": {"windows": {"final_holdout": "2026-09"}}},
        {"seed": True},
    ):
        with pytest.raises(ContractError):
            ExperimentManifest(**{**good, **change})


def test_result_manifest_parsing_is_strict_and_hash_ignores_runtime():
    value = manifest()
    raw = value.to_bytes()
    assert parse_result_manifest(raw) == value
    rerun = manifest(attempt_id=TASK + "-2", lease_token="e" * 32, worker=WorkerInfo(7, (), {}, 9))
    assert rerun.result_hash() == value.result_hash()
    assert manifest(output={"value": 2}).result_hash() != value.result_hash()
    data = json.loads(raw)
    bad = [
        b"{not json",
        raw.replace(b'"value":1', b'"value":NaN'),
        raw[:-1] + b',"schema":"x"}',
        json.dumps({**data, "extra": 1}).encode(),
        json.dumps({**data, "attempt_id": "x" * 34}).encode(),
        json.dumps({**data, "status": "failed"}).encode(),
        json.dumps({**data, "artifacts": [{**data["artifacts"][0], "name": "../x"}]}).encode(),
        json.dumps({k: v for k, v in data.items() if k != "worker"}).encode(),
    ]
    for item in bad:
        with pytest.raises(ContractError):
            parse_result_manifest(item)


def test_registry_is_extensible_and_research_operations_read_staged_inputs(tmp_path):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Params:
        n: int

    try:

        @register_operation("integrator_probe", Params, roles=("baseline_models",))
        def probe(params, ctx):
            return {"n": params.n}

        assert normalized_params("integrator_probe", {"n": 3}) == {"n": 3}
        with pytest.raises(ValueError):
            register_operation("integrator_probe", Params, roles=("baseline_models",))
        with pytest.raises(ValueError):
            register_operation("other_probe", Params, roles=("live",))
    finally:
        operations.REGISTRY.pop("integrator_probe", None)
    ctx = operations.OperationContext(TASK, TASK + "-1", 1, tmp_path, 0.0)
    research = ("train_model", "symbolic_search", "evaluate_candidate")
    research += ("robustness_analysis", "paper_backtest")
    for name in research:
        spec = get_operation(name)
        assert spec.params.__name__.endswith("Params")
        assert spec.inputs is not operations.no_inputs  # every research op reads staged inputs
    params = get_operation("symbolic_search").parse(
        {
            "dataset_manifest_hash": "0" * 64,
            "seed": 0,
            "max_candidates": 10,
            "max_generations": 1,
            "max_seconds": 1.0,
        }
    )
    with pytest.raises(DeterministicFailure, match="not staged"):
        get_operation("symbolic_search").run(params, ctx)  # nothing staged: refused
    with pytest.raises(ContractError):
        normalized_params(
            "train_model",
            {
                "dataset_manifest_hash": "0" * 64,
                "feature_set": "fs-market-v1",
                "feature_set_version": "x",
                "model_family": "logistic",
                "seed": 0,
                "split": "validation",
            },
        )
    with pytest.raises(ContractError):
        normalized_params(
            "evaluate_candidate",
            {"candidate_id": "c" * 32, "dataset_manifest_hash": "0" * 64, "split": "holdout"},
        )


def test_artifact_store_is_write_once_and_verifies_on_read(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    sha = store.put_bytes(b"payload")
    assert store.put_bytes(b"payload") == sha
    path = store.path(sha)
    assert not os.access(path, os.W_OK) or (path.stat().st_mode & 0o222) == 0
    assert store.get_bytes(sha) == b"payload"
    with pytest.raises(ArtifactCorrupted):
        store.put_bytes(b"other", expected_sha256=sha)
    assert sorted(
        p.name for p in (tmp_path / "artifacts" / "sha256").rglob("*") if p.is_file()
    ) == [sha]
    os.chmod(path, 0o644)
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactCorrupted):
        store.get_bytes(sha)
    with pytest.raises(ArtifactCorrupted):
        store.put_bytes(b"payload")  # never silently replaces a corrupted object


def test_scripted_provider_makes_no_network_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    double = providers.ScriptedProvider({"hello": "world"})
    request = providers.ProviderRequest(model="m", prompt="hello")
    first = double.complete(request)
    assert first == double.complete(request)
    assert first.text == "world" and first.cost_usd == 0.0 and len(double.calls) == 2
    with pytest.raises(providers.UnscriptedRequest):
        double.complete(providers.ProviderRequest(model="m", prompt="unknown"))
    with pytest.raises(providers.PaidCallsDisabled):
        providers.UnwiredVendorProvider("any-vendor").complete(request)
    assert "httpx" not in vars(providers) and "socket" not in vars(providers)
