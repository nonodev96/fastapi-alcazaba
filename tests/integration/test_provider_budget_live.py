"""FastAPIProviderBudget against real servers (see test_backends_live for the environment
variables), and its limits in PostgreSQL (POSTGRES_URL). Two budgets on one backend stand in for
two processes. Each test has a random prefix, or a table of its own dropped at its end."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fastapi_alcazaba import Cache
from fastapi_alcazaba.accounting import (
    BudgetLimits,
    BudgetRefused,
    FastAPIProviderBudget,
    SQLBudgetLimits,
    provider_budgets_table,
)
from fastapi_alcazaba.backends import Backend
from tests.integration.test_backends_live import ENV, KINDS, _make

pytestmark = pytest.mark.integration


@pytest.fixture(name="backend", params=KINDS)
async def fixture_backend(request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    url = os.environ.get(ENV[request.param])
    if not url:
        pytest.skip(f"{ENV[request.param]} not set")
    made, close = await _make(request.param, url)
    yield made
    await close()


@pytest.fixture(name="cache")
def fixture_cache(backend: Backend) -> Cache:
    if not backend.supports_counters:
        pytest.skip("no counters")
    return Cache(backend, prefix=f"fastapi-alcazaba-test-{uuid.uuid4().hex}")


def budget(limits: BudgetLimits, total: BudgetLimits | None = None) -> FastAPIProviderBudget:
    return FastAPIProviderBudget("llm", subject=lambda: None, limits=limits, total=total, sinks=())


async def test_two_processes_never_go_past(cache: Cache) -> None:
    """Twenty calls of 100 at once, from two processes, on a quota of 1000 and a total of 800."""
    processes = [budget(BudgetLimits(quota=1_000), BudgetLimits(quota=800)) for _ in range(2)]
    made: list[int] = []
    refused: list[str] = []

    async def one(number: int) -> None:
        try:
            async with processes[number % 2].call("ana", 100, cache=cache) as call:
                await asyncio.sleep(0.02)
                call.use(60)  # 40 of each go back
                made.append(number)
        except BudgetRefused as exc:
            refused.append(exc.reason)

    await asyncio.gather(*(one(number) for number in range(20)))
    # Reserved 100 at a time, 8 fit the total at once; the rest were refused while they ran.
    assert len(made) >= 8
    assert len(made) + len(refused) == 20
    assert (await processes[0].usage("ana", cache=cache)).used == 60 * len(made)
    assert (await processes[1].usage(None, cache=cache)).used == 60 * len(made)


async def test_rate_and_slots(cache: Cache) -> None:
    if not cache.backend.supports_locks:
        pytest.skip("no locks")
    limited = budget(BudgetLimits(rate=2, concurrency=1))
    async with limited.call("ana", cache=cache):
        with pytest.raises(BudgetRefused, match="at once"):
            async with limited.call("ana", cache=cache):
                pass
    with pytest.raises(BudgetRefused, match="Too many calls"):
        async with limited.call("ana", cache=cache):
            pass


@pytest.fixture(name="limits")
async def fixture_limits() -> AsyncIterator[SQLBudgetLimits]:
    url = os.environ.get("POSTGRES_URL")
    if not url:
        pytest.skip("POSTGRES_URL not set")
    engine = create_async_engine(url)
    metadata = MetaData()
    table = provider_budgets_table(metadata, f"budgets_{uuid.uuid4().hex[:12]}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        yield SQLBudgetLimits(async_sessionmaker(engine), table, "llm", plan=lambda user: "pro")
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


async def test_limits_in_postgres(limits: SQLBudgetLimits) -> None:
    await limits.save(BudgetLimits(quota=5_000, rate=60), plan="pro")
    await limits.save(BudgetLimits(quota=2**40), scope="total")
    assert await limits("ana") == BudgetLimits(quota=5_000, rate=60)
    await limits.save(BudgetLimits(quota=0, enabled=False), user="ana")
    assert await limits("ana") == BudgetLimits(quota=0, enabled=False)
    assert (await limits.total()) == BudgetLimits(quota=2**40)
    assert await limits.delete(user="ana")
    assert (await limits("ana")).quota == 5_000  # type: ignore[union-attr]
