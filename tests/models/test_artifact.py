import copy
import hashlib
import json

import numpy as np
import pytest

from vader_intelligence.models import artifact, baselines, calibration, search
from vader_intelligence.models._common import sigmoid
from vader_intelligence.models.artifact import (
    ArtifactError,
    ModelArtifact,
    check_inputs,
    load,
    predict_proba,
)
from vader_intelligence.models.preprocess import FeatureSpec, Preprocessor
from vader_intelligence.provenance import json_text

from ._synthetic import NAMES, SPECS, games

GBM = {
    "learning_rate": 0.1,
    "max_iter": 15,
    "max_leaf_nodes": 7,
    "min_samples_leaf": 10,
    "l2_regularization": 0.0,
}


def fitted(family="logistic"):
    X, y, _, _ = games()
    config = {"C": 1.0} if family == "logistic" else GBM
    preprocessor, params = search.fit_model(family, config, X, y, SPECS, seed=0)
    raw = search.raw_scores(family, preprocessor, params, X)
    cal = calibration.fit_platt(raw, y)
    return ModelArtifact(family, preprocessor, params, cal, {"config": config, "seed": 0}), X


def write_raw(directory, obj, data=None):
    data = json_text(obj).encode() if data is None else data
    path = directory / f"{hashlib.sha256(data).hexdigest()}.json"
    path.write_bytes(data)
    return path


def passthrough(name):
    spec = FeatureSpec(name, missing="forbid", scale=False)
    return Preprocessor((spec,), (None,), (None,), (None,), (False,))


@pytest.mark.parametrize("family", ["logistic", "gbm"])
def test_save_load_round_trip_and_json_shape(tmp_path, family):
    art, X = fitted(family)
    path = art.save(tmp_path)
    data = path.read_bytes()
    assert path.name == f"{art.sha256}.json" and data == art.to_json().encode()
    obj = json.loads(data)
    assert sorted(obj) == sorted(artifact.KEYS) and obj["format"] == "model-artifact-v1"
    assert obj["family"] == family and obj["feature_names"] == NAMES
    assert obj["feature_specs"][2] == {"name": "mid", "missing": "forbid", "scale": False}
    floats = {"learning_rate": "0.10000000000000001", "l2_regularization": "0"}
    assert obj["training"]["config"] == ({"C": "1"} if family == "logistic" else GBM | floats)
    loaded = load(path)
    assert loaded == art and load(path, expected_sha256=art.sha256) == art
    np.testing.assert_array_equal(predict_proba(loaded, X), predict_proba(art, X))


def test_hash_verification(tmp_path):
    art, _ = fitted()
    path = art.save(tmp_path)
    path.write_bytes(path.read_bytes().replace(b'"seed":0', b'"seed":1'))
    with pytest.raises(ArtifactError, match="hash mismatch"):
        load(path)
    renamed = tmp_path / "model.json"
    renamed.write_bytes(art.to_json().encode())
    with pytest.raises(ArtifactError, match="nothing to verify"):
        load(renamed)
    assert load(renamed, expected_sha256=art.sha256) == art
    with pytest.raises(ArtifactError, match="hash mismatch"):
        load(renamed, expected_sha256="0" * 64)
    with pytest.raises(ArtifactError, match="64 lowercase"):
        load(renamed, expected_sha256=art.sha256.upper())


def test_save_is_write_once(tmp_path):
    art, _ = fitted()
    first = art.save(tmp_path)
    assert art.save(tmp_path) == first  # identical content is a no-op
    first.write_bytes(b"{}")
    with pytest.raises(ArtifactError, match="refusing to overwrite"):
        art.save(tmp_path)
    assert first.read_bytes() == b"{}"
    assert sorted(p.name for p in tmp_path.iterdir()) == [first.name]  # no temp files left


def test_unknown_format_family_and_malformed_files_are_refused(tmp_path):
    art, _ = fitted()
    obj = art.to_dict()
    with pytest.raises(ArtifactError, match="unknown artifact format"):
        load(write_raw(tmp_path, obj | {"format": "model-artifact-v2"}))
    with pytest.raises(ArtifactError, match="unknown model family"):
        load(write_raw(tmp_path, obj | {"family": "neural"}))
    with pytest.raises(ArtifactError, match="artifact keys"):
        load(write_raw(tmp_path, obj | {"extra": 1}))
    pretty = json.dumps(obj, indent=1, sort_keys=True).encode()
    with pytest.raises(ArtifactError, match="not canonical"):
        load(write_raw(tmp_path, None, pretty))
    with pytest.raises(ArtifactError, match="not valid JSON"):
        load(write_raw(tmp_path, None, b'{"a":NaN}'))
    floats = copy.deepcopy(obj)
    floats["calibration"]["a"] = 1.5  # a JSON number where a decimal string belongs
    with pytest.raises(ArtifactError, match="round-trip"):
        load(write_raw(tmp_path, floats))
    short = copy.deepcopy(obj)
    short["params"]["coef"].pop()
    with pytest.raises(ArtifactError, match="invalid logistic artifact"):
        load(write_raw(tmp_path, short))
    renamed = copy.deepcopy(obj)
    renamed["feature_names"][0] = "other"
    with pytest.raises(ArtifactError, match="disagree"):
        load(write_raw(tmp_path, renamed))


def test_feature_mismatch_is_refused():
    art, X = fitted()
    check_inputs(art, NAMES)
    for names in [NAMES[::-1], NAMES[:2], NAMES + ["extra"]]:
        with pytest.raises(ArtifactError, match="feature mismatch"):
            check_inputs(art, names)
    with pytest.raises(ArtifactError, match="feature mismatch"):
        predict_proba(art, X[:, :2])
    missing_mid = X.copy()
    missing_mid[0, 2] = np.nan
    with pytest.raises(ValueError, match="forbid"):
        predict_proba(art, missing_mid)


def test_numbers_are_canonicalized_and_bad_contents_refused():
    art, _ = fitted()
    training = {"lr": np.float64(0.1), "n": np.int64(3), "ok": np.bool_(True), "v": np.array([0.5])}
    other = ModelArtifact(art.family, art.preprocessor, art.params, art.calibration, training)
    assert other.training == {"lr": "0.10000000000000001", "n": 3, "ok": True, "v": ["0.5"]}
    for bad in [{"x": float("nan")}, {1: "a"}, {"x": object()}]:
        with pytest.raises(ArtifactError):
            ModelArtifact(art.family, art.preprocessor, art.params, art.calibration, bad)
    with pytest.raises(ArtifactError, match="unknown model family"):
        ModelArtifact("neural", art.preprocessor, art.params, art.calibration, {})
    with pytest.raises(ArtifactError, match="invalid gbm artifact"):
        ModelArtifact("gbm", art.preprocessor, art.params, art.calibration, {})
    with pytest.raises(ArtifactError, match="invalid logistic artifact"):
        ModelArtifact("logistic", art.preprocessor, art.params, {"kind": "beta"}, {})


def symbolic_scorer(params, Z, names):
    return float(params["a"]) * Z[:, names.index(params["expression"]["var"])] + float(params["b"])


def test_registered_family_plug_in(tmp_path, monkeypatch):
    # Import (and so register) the real symbolic family first: imported for the first
    # time under the patch, it would register into the throwaway dict below.
    import vader_intelligence.symbolic.artifact_family  # noqa: F401

    monkeypatch.setattr(artifact, "_REGISTERED", {})
    params = {"expression": {"var": "x"}, "a": 2.0, "b": -1.0, "operator_set": "ops-v1"}
    art = ModelArtifact("symbolic", passthrough("x"), params, calibration.identity(), {})
    assert art.params["a"] == "2" and art.params["b"] == "-1"
    X = np.array([[0.0], [0.5], [2.0]])
    with pytest.raises(ArtifactError, match="no scorer registered"):
        predict_proba(art, X)
    path = art.save(tmp_path)  # loadable before registration: symbolic is a known family
    artifact.register_family("symbolic", symbolic_scorer)
    artifact.register_family("symbolic", symbolic_scorer)  # same function: no-op
    np.testing.assert_allclose(predict_proba(load(path), X), sigmoid(2 * X[:, 0] - 1), rtol=1e-15)
    with pytest.raises(ValueError, match="already registered"):
        artifact.register_family("symbolic", lambda params, Z, names: Z[:, 0])
    with pytest.raises(ValueError, match="built in"):
        artifact.register_family("gbm", symbolic_scorer)
    with pytest.raises(ValueError, match="invalid family name"):
        artifact.register_family("Bad Name", symbolic_scorer)
    custom = write_raw(tmp_path, art.to_dict() | {"family": "custom"})
    with pytest.raises(ArtifactError, match="unknown model family"):
        load(custom)
    artifact.register_family("custom", symbolic_scorer)
    assert load(custom).family == "custom"
    artifact.register_family("broken", lambda params, Z, names: np.full(len(Z), np.nan))
    broken = ModelArtifact("broken", passthrough("x"), params, calibration.identity(), {})
    with pytest.raises(ArtifactError, match="invalid raw scores"):
        predict_proba(broken, X)


def test_symbolic_params_are_validated_at_construction(tmp_path):
    """The real symbolic family parses params["model"] and requires its feature names to
    be the preprocessor outputs, in order, before an artifact exists (not at prediction)."""
    from vader_intelligence.symbolic import Model, Op, Var, model_to_dict

    X, _, _, _ = games()
    preprocessor = Preprocessor.fit(X, SPECS)
    names = preprocessor.output_names
    expression = Op("sub", (Var("mid"), Var("strength")))
    model = model_to_dict(Model(expression, 2.0, -1.0, tuple(names)))
    good = ModelArtifact("symbolic", preprocessor, {"model": model}, calibration.identity(), {})
    assert load(good.save(tmp_path)) == good
    Z = preprocessor.transform(X[:5])
    expected = sigmoid(2.0 * (Z[:, names.index("mid")] - Z[:, names.index("strength")]) - 1.0)
    np.testing.assert_allclose(predict_proba(good, X[:5]), expected, rtol=1e-12)
    assert predict_proba(good, np.zeros((0, len(NAMES)))).shape == (0,)  # verification call
    bad = [
        {},
        {"model": {"bogus": 1}},
        {"model": dict(model, feature_names=list(reversed(names)))},
        {"model": dict(model, feature_names=names[:-1])},
        {"model": dict(model, operator_set="ops-v0")},
        {"model": model, "extra": 1},
        {"model": "x"},
    ]
    for params in bad:
        with pytest.raises(ArtifactError, match="invalid symbolic artifact"):
            ModelArtifact("symbolic", preprocessor, params, calibration.identity(), {})
        with pytest.raises(ArtifactError, match="invalid symbolic artifact"):
            load(write_raw(tmp_path, good.to_dict() | {"params": params}))


def test_market_baselines_are_scored_like_models(tmp_path):
    mid = baselines.market_mid("mid", training={"dataset": "synthetic"})
    const = baselines.constant("mid")
    X = np.array([[0.0], [0.3], [0.75], [1.0]])
    np.testing.assert_allclose(predict_proba(mid, X), [1e-6, 0.3, 0.75, 1 - 1e-6], rtol=1e-12)
    np.testing.assert_array_equal(predict_proba(const, X), 0.5)
    for art in (mid, const):
        assert art.family == "market_baseline" and load(art.save(tmp_path)) == art
        check_inputs(art, ["mid"])
        with pytest.raises(ValueError, match="forbid"):
            predict_proba(art, np.array([[np.nan]]))
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        predict_proba(mid, np.array([[1.2]]))
    with pytest.raises(ArtifactError, match="invalid market_baseline"):
        ModelArtifact(
            "market_baseline", passthrough("mid"), {"kind": "median"}, {"kind": "identity"}, {}
        )
