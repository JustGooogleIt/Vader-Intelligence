import json

import numpy as np
import pytest
from sklearn.isotonic import IsotonicRegression

from vader_intelligence.models import calibration
from vader_intelligence.models._common import sigmoid
from vader_intelligence.provenance import json_text


def platt_data(seed=0, n=20000, a=2.0, b=-0.5):
    rng = np.random.default_rng(seed)
    s = rng.normal(size=n)
    return s, (rng.random(n) < sigmoid(a * s + b)).astype(np.int64)


def round_trip(cal):
    return json.loads(json_text(cal))


def test_platt_recovers_known_map_and_round_trips():
    s, y = platt_data()
    cal = calibration.fit_platt(s, y)
    assert set(cal) == {"kind", "a", "b", "l2", "n_iter"} and 0 < cal["n_iter"] < 100
    assert abs(float(cal["a"]) - 2.0) < 0.1 and abs(float(cal["b"]) + 0.5) < 0.1
    grid = np.linspace(-8, 8, 101)
    p = calibration.apply(round_trip(cal), grid)
    np.testing.assert_array_equal(p, calibration.apply(cal, grid))
    assert (np.diff(p) > 0).all() and ((p > 0) & (p < 1)).all()
    assert json_text(cal) == json_text(calibration.fit_platt(s, y))


def test_platt_converges_on_separable_scores_and_bounds_iterations():
    s = np.linspace(-3, 3, 200)
    y = (s > 0).astype(np.int64)
    cal = calibration.fit_platt(s, y)
    assert float(cal["a"]) > 1 and np.isfinite(float(cal["a"]))
    with pytest.raises(ValueError, match="converge"):
        calibration.fit_platt(*platt_data(n=2000), max_iter=1)
    with pytest.raises(ValueError, match="both classes"):
        calibration.fit_platt(s, np.ones(200, dtype=int))
    with pytest.raises(ValueError):
        calibration.fit_platt(s, y, l2=0.0)


def test_isotonic_monotone_clipped_round_trip_with_sklearn_parity():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.05, 0.95, 3000)
    y = (rng.random(3000) < p**2).astype(np.int64)
    y[p < 0.2] = 0  # a flat region the bounds must lift off zero
    cal = calibration.fit_isotonic(p, y)
    assert float(cal["parity_max_abs_error"]) <= 1e-9
    model = IsotonicRegression(out_of_bounds="clip", y_min=1e-6, y_max=1 - 1e-6).fit(p, y)
    raw = np.linspace(-12, 12, 1001)  # extends beyond the training range on both ends
    calibrated = calibration.apply(round_trip(cal), raw)
    np.testing.assert_allclose(calibrated, model.predict(sigmoid(raw)), rtol=0, atol=1e-12)
    assert (np.diff(calibrated) >= 0).all()
    assert calibrated.min() == 1e-6 and calibrated.max() <= 1 - 1e-6
    extremes = calibration.apply(cal, np.array([-50.0, 50.0]))
    assert extremes[0] == float(cal["y"][0]) and extremes[1] == float(cal["y"][-1])


def test_identity_and_malformed_calibrations():
    raw = np.array([-2.0, 0.0, 3.0])
    np.testing.assert_array_equal(calibration.apply(calibration.identity(), raw), sigmoid(raw))
    cal = calibration.fit_platt(*platt_data(n=500))
    for bad in [
        {"kind": "beta"},
        {"kind": "identity", "extra": 1},
        cal | {"a": "2.0"},  # not the canonical 17-digit form
        {k: v for k, v in cal.items() if k != "l2"},
    ]:
        with pytest.raises(ValueError):
            calibration.apply(bad, raw)
    with pytest.raises(ValueError):
        calibration.apply(cal, np.array([np.nan]))
