"""Fixtures for pod tests; helpers live in `_harness` so test modules can import them."""

import pytest

from ._harness import Harness


@pytest.fixture
def pods(tmp_path):
    harness = Harness(tmp_path / "orch")
    yield harness
    harness.close()
