from collections.abc import Iterator
from pathlib import Path

import pytest

from fastapi_alcazaba import FastAPICache

SUITE_MARKERS = {"integration": "integration", "e2e": "e2e", "benchmarks": "benchmark"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Each suite's directory gives its tests a marker: `pytest -m integration` and so on."""
    tests_dir = Path(__file__).parent
    for item in items:
        suite = item.path.relative_to(tests_dir).parts[0]
        if suite in SUITE_MARKERS:
            item.add_marker(SUITE_MARKERS[suite])


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """FastAPICache is global: no test sees another one's configuration."""
    FastAPICache.reset()
    yield
    FastAPICache.reset()
