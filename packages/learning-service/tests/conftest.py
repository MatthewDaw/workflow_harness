"""Shared pytest wiring for the learning-service test suite.

``slow`` tests (real model load) are opt-in; the default ``pytest`` run is the
offline gate.
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="run tests marked slow (they load the real NLI/judge model)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(
        reason="loads the real NLI/judge model; opt in with --run-slow"
    )
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
