"""Shared pytest fixtures."""

import pytest

from plants.sub300 import model


@pytest.fixture(scope="session")
def params():
    """Plain design parameters of the sub300 unit."""
    return model.load_params()


@pytest.fixture(scope="session")
def solved(params):
    """Solved design network and its summary (do not modify)."""
    return model.run(params)


@pytest.fixture(scope="session")
def network(solved):
    return solved[0]


@pytest.fixture(scope="session")
def summary(solved):
    return solved[1]
