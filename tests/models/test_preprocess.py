import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

from vader_intelligence.models.preprocess import FeatureSpec, Preprocessor

SPECS = [
    FeatureSpec("a"),
    FeatureSpec("b", missing="zero_indicator"),
    FeatureSpec("c", missing="forbid"),
    FeatureSpec("d", scale=False),
]


def matrix(seed, n=40):
    rng = np.random.default_rng(seed)
    X = rng.normal(loc=3.0, scale=2.0, size=(n, 4))
    X[rng.random(n) < 0.25, 0] = np.nan
    X[rng.random(n) < 0.25, 1] = np.nan
    X[rng.random(n) < 0.25, 3] = np.nan
    return X


def test_fit_uses_training_rows_only():
    X = matrix(0)
    train, test = np.arange(30), np.arange(30, 40)
    reference = Preprocessor.fit(X[train], SPECS).to_dict()
    changed = X.copy()
    changed[test] = np.random.default_rng(1).normal(loc=100.0, size=(10, 4))
    fitted = Preprocessor.fit(changed[train], SPECS)
    assert fitted.to_dict() == reference
    np.testing.assert_array_equal(
        fitted.transform(changed[train]), Preprocessor.fit(X[train], SPECS).transform(X[train])
    )


@settings(max_examples=40, deadline=None)
@given(
    arrays(
        np.float64,
        st.tuples(st.integers(1, 25), st.just(4)),
        elements=st.one_of(st.integers(-(10**6), 10**6).map(lambda v: v / 64), st.just(np.nan)),
    )
)
def test_treatments_and_indicators_property(X):
    X[:, 2] = np.nan_to_num(X[:, 2])  # forbid column has no missing values
    pre = Preprocessor.fit(X, SPECS)
    Z = pre.transform(X)
    assert pre.output_names == ["a", "b", "c", "d", "a__missing", "b__missing", "d__missing"]
    assert Z.shape == (len(X), 7) and np.isfinite(Z).all()
    for j, col in zip((0, 1, 3), (4, 5, 6)):
        np.testing.assert_array_equal(Z[:, col], np.isnan(X[:, j]).astype(float))
    observed = X[~np.isnan(X[:, 0]), 0]
    assert pre.fill[0] == (float(np.median(observed)) if len(observed) else 0.0)
    assert pre.fill[1] == 0.0 and pre.fill[2] is None
    for j in (0, 1, 2):
        if not pre.constant[j]:
            assert abs(Z[:, j].mean()) < 1e-6 and abs(Z[:, j].std() - 1) < 1e-6
    unscaled = np.where(np.isnan(X[:, 3]), pre.fill[3], X[:, 3])
    np.testing.assert_array_equal(Z[:, 3], unscaled)
    assert Preprocessor.from_dict(pre.to_dict()) == pre


def test_indicators_exist_even_without_missing_training_values():
    X = np.nan_to_num(matrix(2))
    pre = Preprocessor.fit(X, SPECS)
    assert len(pre.output_names) == 7
    test = X[:3].copy()
    test[0, 0] = np.nan
    Z = pre.transform(test)
    assert Z[0, 4] == 1.0 and Z[0, 0] == (pre.fill[0] - pre.mean[0]) / pre.std[0]


def test_constant_columns_are_centered_not_scaled():
    X = matrix(3)
    X[:, 0] = 7.0
    pre = Preprocessor.fit(X, SPECS)
    assert pre.constant[0] and pre.std[0] < 1e-12
    Z = pre.transform(np.vstack([X[:2], [9.0, 1.0, 1.0, 1.0]]))
    np.testing.assert_array_equal(Z[:, 0], [0.0, 0.0, 2.0])


def test_all_missing_median_feature_fills_zero():
    X = matrix(4)
    X[:, 0] = np.nan
    pre = Preprocessor.fit(X, SPECS)
    assert pre.fill[0] == 0.0 and pre.constant[0]


def test_forbid_raises_in_fit_and_transform():
    X = matrix(5)
    bad = X.copy()
    bad[3, 2] = np.nan
    with pytest.raises(ValueError, match="forbid"):
        Preprocessor.fit(bad, SPECS)
    pre = Preprocessor.fit(X, SPECS)
    with pytest.raises(ValueError, match="forbid"):
        pre.transform(bad)


def test_invalid_inputs_and_serialized_forms_are_refused():
    X = matrix(6)
    with pytest.raises(ValueError):
        Preprocessor.fit(X[:, :3], SPECS)
    infinite = X.copy()
    infinite[0, 0] = np.inf
    with pytest.raises(ValueError, match="infinite"):
        Preprocessor.fit(infinite, SPECS)
    with pytest.raises(ValueError):
        Preprocessor.fit(X[:0], SPECS)
    with pytest.raises(ValueError):
        FeatureSpec("x", missing="mean")
    with pytest.raises(ValueError, match="duplicate"):
        Preprocessor.fit(X[:, :2], [FeatureSpec("a"), FeatureSpec("a__missing")])
    data = Preprocessor.fit(X, SPECS).to_dict()
    data["output_names"] = data["output_names"][::-1]
    with pytest.raises(ValueError):
        Preprocessor.from_dict(data)
    data = Preprocessor.fit(X, SPECS).to_dict()
    data["features"][0]["constant"] = True
    with pytest.raises(ValueError, match="constant"):
        Preprocessor.from_dict(data)
    data = Preprocessor.fit(X, SPECS).to_dict()
    data["features"][1]["fill"] = "0.5"
    with pytest.raises(ValueError, match="zero_indicator"):
        Preprocessor.from_dict(data)
