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


@pytest.fixture(scope="session")
def state(network):
    """Design state of the sub300 unit for off-design calculations."""
    return model.design_state(network)


@pytest.fixture(scope="session")
def sweep(params):
    """Part-load summaries of sub300 with the default operating mode."""
    return model.load_sweep(params, loads=(1.0, 0.9, 0.75, 0.5, 0.4))
