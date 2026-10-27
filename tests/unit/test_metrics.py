from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.response_cache import cache


@pytest.fixture(name="reader")
def fixture_reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


def points(reader: InMemoryMetricReader, name: str) -> list[tuple[dict[str, Any], Any]]:
    data = reader.get_metrics_data()
    found = []
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        value = getattr(point, "value", None)
                        found.append(
                            (
                                dict(point.attributes or {}),
                                value if value is not None else getattr(point, "count", None),
                            )
                        )
    return found


class FailingSet(InMemoryBackend):
    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        raise ConnectionError("down")


async def test_lookups_stores_and_backend_calls(reader: InMemoryMetricReader) -> None:
    FastAPICache.init(InMemoryBackend(), meter_provider=MeterProvider(metric_readers=[reader]))

    @cache(expire=60, namespace="products")
    async def value() -> int:
        return 1

    await value()
    await value()
    lookups = {
        attrs["fastapi_alcazaba.result"]: count
        for attrs, count in points(reader, "fastapi_alcazaba.lookups")
    }
    assert lookups == {"miss": 1, "hit": 1}
    stores = points(reader, "fastapi_alcazaba.stores")
    assert stores == [
        ({"fastapi_alcazaba.outcome": "stored", "fastapi_alcazaba.namespace": "products"}, 1)
    ]
    operations = {
        attrs["fastapi_alcazaba.operation"]
        for attrs, _ in points(reader, "fastapi_alcazaba.backend.duration")
    }
    assert operations == {"get", "set"}
    assert points(reader, "fastapi_alcazaba.entry.size")


async def test_backend_errors(reader: InMemoryMetricReader) -> None:
    FastAPICache.init(FailingSet(), meter_provider=MeterProvider(metric_readers=[reader]))

    @cache(expire=60)
    async def value() -> int:
        return 1

    await value()
    assert points(reader, "fastapi_alcazaba.backend.errors") == [
        ({"fastapi_alcazaba.operation": "set", "error.type": "ConnectionError"}, 1)
    ]
    assert points(reader, "fastapi_alcazaba.stores")[0][0]["fastapi_alcazaba.outcome"] == "failed"


async def test_metrics_can_be_turned_off(reader: InMemoryMetricReader) -> None:
    FastAPICache.init(
        InMemoryBackend(), metrics=False, meter_provider=MeterProvider(metric_readers=[reader])
    )

    @cache(expire=60)
    async def value() -> int:
        return 1

    await value()
    assert points(reader, "fastapi_alcazaba.lookups") == []
