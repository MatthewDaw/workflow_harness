"""Shared pytest wiring: `slow` (real embedding model) tests are opt-in.

The default `uv run pytest` is the offline gate the loop runs every iteration;
`uv run pytest --run-slow` adds the real-model integration tests (local, free,
but ~0.5 GB model cache and tens of seconds of load time).
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="run tests marked slow (they load the real embedding model)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(
        reason="loads the real embedding model; opt in with --run-slow"
    )
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
