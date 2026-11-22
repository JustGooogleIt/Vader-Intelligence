import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vader_intelligence.models._common import decode, encode, logit, sigmoid


@settings(max_examples=200, deadline=None)
@given(st.floats(allow_nan=False, allow_infinity=False))
def test_decimal_strings_round_trip_exactly(value):
    text = encode(value)
    assert decode(text) == value and math.copysign(1, decode(text)) == math.copysign(1, value)
    assert encode(decode(text)) == text


def test_decimal_strings_are_17_significant_digits_and_strict():
    assert encode(0.1) == "0.10000000000000001"
    assert encode(0.5) == "0.5" and encode(np.float32(0.5)) == "0.5"
    assert decode("0.10000000000000001") == 0.1
    for bad in ["0.1", "1e-5", " 0.5", "0.50", 0.5, "nan"]:
        with pytest.raises(ValueError):
            decode(bad)
    for bad in [math.nan, math.inf]:
        with pytest.raises(ValueError):
            encode(bad)
    with pytest.raises(TypeError):
        encode(True)
    assert decode(encode(math.inf, allow_inf=True), allow_inf=True) == math.inf
    with pytest.raises(ValueError):
        decode("inf")


def test_sigmoid_is_stable_and_inverts_logit():
    z = np.array([-1000.0, -30.0, 0.0, 30.0, 1000.0])
    p = sigmoid(z)
    assert np.isfinite(p).all() and p[0] == 0.0 and p[2] == 0.5 and p[-1] == 1.0
    q = np.array([1e-6, 0.25, 0.5, 0.75, 1 - 1e-6])
    np.testing.assert_allclose(sigmoid(logit(q)), q, rtol=1e-12)
