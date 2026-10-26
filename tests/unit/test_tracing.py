import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fastapi_alcazaba import Cache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend


@pytest.fixture(name="exporter")
def fixture_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def tracer_provider(exporter: InMemorySpanExporter) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def by_name(spans: tuple[ReadableSpan, ...], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]


async def test_spans_of_a_miss_and_a_hit(exporter: InMemorySpanExporter) -> None:
    app_cache = Cache(InMemoryBackend(), tracer_provider=tracer_provider(exporter))

    @app_cache.cached(expire=60, namespace="products")
    async def products() -> list[int]:
        return [1, 2]

    await products()
    await products()
    spans = exporter.get_finished_spans()
    calls = by_name(spans, "fastapi_alcazaba test_spans_of_a_miss_and_a_hit.<locals>.products")
    assert [call.attributes["fastapi_alcazaba.result"] for call in calls] == ["miss", "hit"]  # type: ignore[index]
    assert calls[0].attributes["fastapi_alcazaba.namespace"] == "products"  # type: ignore[index]
    gets, sets = (
        by_name(spans, "fastapi_alcazaba.backend get"),
        by_name(spans, "fastapi_alcazaba.backend set"),
    )
    assert len(gets) == 2
    assert len(sets) == 1
    # Backend calls are children of the cached call they belong to.
    assert sets[0].parent is not None
    assert calls[0].context is not None
    assert sets[0].parent.span_id == calls[0].context.span_id


async def test_backend_failures_are_recorded(exporter: InMemorySpanExporter) -> None:
    class Down(InMemoryBackend):
        async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
            raise ConnectionError("down")

    app_cache = Cache(Down(), tracer_provider=tracer_provider(exporter))

    @app_cache.cached(expire=60)
    async def value() -> int:
        return 1

    assert await value() == 1
    (get,) = by_name(exporter.get_finished_spans(), "fastapi_alcazaba.backend get")
    assert get.status.status_code is StatusCode.ERROR
    assert get.events[0].name == "exception"


async def test_tracing_can_be_turned_off(exporter: InMemorySpanExporter) -> None:
    app_cache = Cache(InMemoryBackend(), tracing=False, tracer_provider=tracer_provider(exporter))

    @app_cache.cached(expire=60)
    async def value() -> int:
        return 1

    await value()
    assert exporter.get_finished_spans() == ()
