"""FastAPIProviderBudget: what each user, and the whole app, may spend at a provider; reserved
before each call and settled after it, with limits read from a database and kept a while."""

import asyncio
from collections.abc import Callable, Iterator
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import MetaData, create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fastapi_alcazaba import Cache
from fastapi_alcazaba.accounting import (
    BudgetLimits,
    BudgetRefused,
    FastAPIProviderBudget,
    MemoryUsageSink,
    ProviderCall,
    SQLBudgetLimits,
    provider_budgets_table,
)
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.telemetry import Metrics
from tests.unit.test_metrics import points


def user(x_user: Annotated[str | None, Header()] = None) -> str | None:
    """Stands in for the app's authentication: the user the request says it is."""
    return x_user


class Setup:
    """A budget, its cache and records, and an app whose endpoint calls the provider."""

    def __init__(self, limits: Any = None, **options: Any) -> None:
        self.cache = Cache(options.pop("backend", None) or InMemoryBackend())
        self.sink = MemoryUsageSink()
        options.setdefault("sinks", [self.sink])
        self.budget = FastAPIProviderBudget(
            "llm",
            subject=user,
            limits=limits if limits is not None else BudgetLimits(quota=1_000),
            cache=self.cache,
            **options,
        )
        self.app = FastAPI()
        budget = self.budget

        @self.app.post("/summaries")
        async def summarize(
            call: Annotated[ProviderCall, Depends(budget.reserve(300, at_least=50))],
            used: int = 100,
            fail: bool = False,
        ) -> dict[str, int]:
            if fail:
                raise RuntimeError("the provider is down")
            call.use(min(used, call.reserved))
            return {"reserved": call.reserved, "used": call.used}

        self.client = TestClient(self.app, raise_server_exceptions=False)

    def post(self, name: str = "ana", **params: Any) -> Any:
        return self.client.post("/summaries", params=params, headers={"X-User": name})

    async def used(self, name: str | None = "ana") -> int:
        return (await self.budget.usage(name, cache=self.cache)).used


class TestReservations:
    async def test_reserved_then_settled(self) -> None:
        setup = Setup()
        response = setup.post(used=120)
        assert response.json() == {"reserved": 300, "used": 120}
        assert await setup.used() == 120  # the 180 not used went back
        (record,) = setup.sink.records
        assert (record.subject, record.meter, record.amount) == ("ana", "llm", 120)
        assert record.route == "/summaries"
        assert response.headers["RateLimit"].startswith('"llm";r=700;')
        assert response.headers["RateLimit-Policy"] == '"llm";q=1000'

    async def test_what_is_left_down_to_at_least(self) -> None:
        setup = Setup()
        for _ in range(9):
            setup.post(used=100)
        assert setup.post(used=500).json() == {"reserved": 100, "used": 100}  # 100 left
        refused = setup.post()
        assert refused.status_code == 402
        assert int(refused.headers["Retry-After"]) > 0
        assert await setup.used() == 1_000

    async def test_calls_at_the_same_time_never_go_past(self) -> None:
        """Reserved before each call: twenty calls of 100 at once against a quota of 1000."""
        setup = Setup()
        done = 0
        refused: list[str] = []

        async def one() -> None:
            nonlocal done
            try:
                async with setup.budget.call("ana", 100, cache=setup.cache) as call:
                    await asyncio.sleep(0.01)
                    call.use(100)
                    done += 1
            except BudgetRefused as exc:
                refused.append(exc.reason)

        await asyncio.gather(*(one() for _ in range(20)))
        assert (done, refused) == (10, ["quota"] * 10)
        assert await setup.used() == 1_000

    async def test_a_failed_call_gives_everything_back(self) -> None:
        setup = Setup()
        assert setup.post(fail=True).status_code == 500
        assert await setup.used() == 0
        assert list(setup.sink.records) == []

    async def test_what_was_used_stays_spent_even_when_it_fails(self) -> None:
        setup = Setup()

        async def used_then_failed() -> None:
            async with setup.budget.call("ana", 300, cache=setup.cache) as call:
                call.use(40)
                raise RuntimeError("the answer couldn't be parsed")

        with pytest.raises(RuntimeError):
            await used_then_failed()
        assert await setup.used() == 40
        assert [record.amount for record in setup.sink.records] == [40]

    async def test_more_than_reserved_is_charged(self, caplog: pytest.LogCaptureFixture) -> None:
        setup = Setup()
        async with setup.budget.call("ana", 100, cache=setup.cache) as call:
            call.use(130)
        assert await setup.used() == 130
        assert "used 130 units, 100 reserved" in caplog.text

    async def test_max_per_call(self) -> None:
        setup = Setup(BudgetLimits(quota=1_000, max_per_call=80))
        assert setup.post(used=500).json() == {"reserved": 80, "used": 80}

    async def test_without_a_quota(self) -> None:
        setup = Setup(BudgetLimits())
        assert setup.post(used=250).json() == {"reserved": 300, "used": 250}
        assert await setup.used() == 250
        assert "RateLimit" not in setup.post().headers

    def test_without_a_user(self) -> None:
        setup = Setup()
        assert setup.client.post("/summaries").status_code == 401


class TestTheWholeApp:
    async def test_the_total_is_shared(self) -> None:
        setup = Setup(BudgetLimits(quota=1_000), total=BudgetLimits(quota=400))
        assert setup.post("ana", used=300).json() == {"reserved": 300, "used": 300}
        # Ben has a quota of his own left, the app only 100: he gets 100.
        ben = setup.post("ben", used=300)
        assert ben.json() == {"reserved": 100, "used": 100}
        assert ben.headers["RateLimit"].startswith('"llm";r=900;')  # his 100, not the 300 asked
        assert await setup.used("ben") == 100  # what the app didn't have went back to him
        refused = setup.post("cy")
        assert refused.status_code == 402
        assert await setup.used(None) == 400
        assert await setup.used("cy") == 0

    async def test_disabled(self) -> None:
        setup = Setup(BudgetLimits(enabled=False))
        assert setup.post().status_code == 403
        app_off = Setup(total=BudgetLimits(enabled=False))
        assert app_off.post().status_code == 403

    async def test_total_rate_and_concurrency(self) -> None:
        setup = Setup(BudgetLimits(), total=BudgetLimits(rate=2))
        assert [setup.post(name).status_code for name in ("a", "b", "c")] == [200, 200, 429]


class TestRateAndConcurrency:
    def test_rate(self) -> None:
        setup = Setup(BudgetLimits(rate=2, rate_window=60))
        statuses = [setup.post().status_code for _ in range(3)]
        assert statuses == [200, 200, 429]
        assert setup.post("ben").status_code == 200  # each user has their own
        assert 0 < int(setup.post().headers["Retry-After"]) <= 60

    async def test_concurrency(self) -> None:
        setup = Setup(BudgetLimits(concurrency=1))
        inside = asyncio.Event()

        async def first() -> None:
            async with setup.budget.call("ana", cache=setup.cache):
                inside.set()
                await asyncio.sleep(0.05)

        task = asyncio.create_task(first())
        await inside.wait()
        with pytest.raises(BudgetRefused) as refused:
            async with setup.budget.call("ana", cache=setup.cache):
                pass
        assert (refused.value.status_code, refused.value.reason) == (429, "concurrency")
        async with setup.budget.call("ben", cache=setup.cache):  # another user's slot
            pass
        await task
        async with setup.budget.call("ana", cache=setup.cache):  # freed
            pass


class TestLimitsFromTheDatabase:
    async def test_read_once_per_settings_ttl_and_invalidated(self) -> None:
        reads: list[str] = []
        plans = {"ana": BudgetLimits(quota=1_000)}

        async def limits_of(name: str) -> BudgetLimits | None:
            reads.append(name)
            return plans.get(name)

        setup = Setup(limits_of, settings_ttl=60, default=BudgetLimits(quota=100))
        for _ in range(3):
            setup.post()
        assert reads == ["ana"]
        plans["ana"] = BudgetLimits(quota=0)  # the admin panel cuts her off
        assert setup.post().status_code == 200  # still the limits kept
        setup.budget.invalidate("ana")
        assert setup.post().status_code == 402
        assert reads == ["ana", "ana"]
        assert setup.post("ben").json()["reserved"] == 100  # no row: the default

    async def test_the_last_limits_while_the_database_is_down(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        down = [False]

        def limits_of(name: str) -> BudgetLimits:
            del name
            if down[0]:
                raise ConnectionError("the database is down")
            return BudgetLimits(quota=1_000)

        setup = Setup(limits_of, settings_ttl=0)
        assert setup.post().status_code == 200
        down[0] = True
        assert setup.post().status_code == 200  # the ones read last
        assert "the last ones are kept" in caplog.text
        refused = setup.post("ben")  # never read: nothing to keep
        assert refused.status_code == 503

    async def test_wrong_limits(self) -> None:
        setup = Setup(lambda name: {"quota": 5})
        assert setup.post().status_code == 503

    async def test_invalidate_everything_and_the_total(self) -> None:
        totals = [BudgetLimits(quota=100)]
        setup = Setup(total=lambda: totals[0])
        assert setup.post(used=100).json()["reserved"] == 100
        totals[0] = BudgetLimits(quota=1_000)
        setup.budget.invalidate(total=True)
        assert setup.post().status_code == 200
        totals[0] = BudgetLimits(enabled=False)  # the provider turned off for everybody
        setup.budget.invalidate()  # everything: the total too
        assert setup.post().status_code == 403


class TestUsageAndReset:
    async def test_usage_and_reset(self) -> None:
        setup = Setup(total=BudgetLimits(quota=5_000))
        setup.post(used=200)
        usage = await setup.budget.usage("ana", cache=setup.cache)
        assert (usage.used, usage.limit, usage.remaining) == (200, 1_000, 800)
        assert (await setup.budget.usage(None, cache=setup.cache)).remaining == 4_800
        await setup.budget.reset("ana", cache=setup.cache)
        assert await setup.used() == 0
        await setup.budget.reset(None, cache=setup.cache)
        assert await setup.used(None) == 0
        assert (await Setup(BudgetLimits()).budget.usage(None)).remaining is None


class Down(InMemoryBackend):
    async def incr_window(self, *args: Any, **kwargs: Any) -> tuple[bool, int, int]:
        raise ConnectionError("down")


class TestWhenTheBackendFails:
    def test_closed_by_default(self) -> None:
        setup = Setup(backend=Down())
        refused = setup.post()
        assert refused.status_code == 503
        assert list(setup.sink.records) == []

    async def test_open_when_asked(self) -> None:
        setup = Setup(backend=Down(), fail_open=True)
        assert setup.post(used=70).json() == {"reserved": 300, "used": 70}
        assert [record.amount for record in setup.sink.records] == [70]  # billing still has it

    def test_rate_and_slots(self) -> None:
        assert Setup(BudgetLimits(rate=5), backend=Down()).post().status_code == 503
        opened = Setup(BudgetLimits(rate=5), backend=Down(), fail_open=True)
        assert opened.post().status_code == 200


async def test_metrics() -> None:
    reader = InMemoryMetricReader()
    metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
    setup = Setup(BudgetLimits(quota=300), metrics=metrics)
    setup.post(used=300)
    setup.post()
    setup.post(fail=True)
    outcomes = {
        a["fastapi_alcazaba.outcome"]: v for a, v in points(reader, "fastapi_alcazaba.budget")
    }
    assert outcomes == {"reserved": 1, "exceeded": 2}
    assert points(reader, "fastapi_alcazaba.budget.usage") == [
        ({"fastapi_alcazaba.provider": "llm"}, 300)
    ]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"quota": -1}, "negative"),
        ({"quota": 1.5}, "whole number"),
        ({"rate": 0}, "at least 1"),
        ({"period": "week"}, "period"),
        ({"rate_window": 0}, "rate_window"),
    ],
)
def test_invalid_limits(options: dict[str, Any], message: str) -> None:
    with pytest.raises((ValueError, TypeError), match=message):
        BudgetLimits(**options)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"provider": ""}, "provider"),
        ({"settings_ttl": -1}, "settings_ttl"),
        ({"cache_size": 0}, "cache_size"),
        ({"exceeded_status": 403}, "402 or 429"),
        ({"wait": -1}, "wait"),
        ({"timeout": 0}, "timeout"),
    ],
)
def test_invalid_budgets(options: dict[str, Any], message: str) -> None:
    arguments = {"provider": "llm", "subject": user, "limits": BudgetLimits(), **options}
    with pytest.raises(ValueError, match=message):
        FastAPIProviderBudget(**arguments)


async def test_invalid_calls() -> None:
    budget = Setup().budget
    with pytest.raises(ValueError, match="units"):
        budget.reserve(0)
    with pytest.raises(ValueError, match="at_least"):
        budget.reserve(10, at_least=11)
    with pytest.raises(ValueError, match="user's ID"):
        async with budget.call(None, 1):  # type: ignore[arg-type]
            pass


@pytest.fixture(name="store")
def fixture_store() -> Iterator[Callable[..., SQLBudgetLimits]]:
    """Makes stores on one in-memory database, closed after the test."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    metadata = MetaData()
    table = provider_budgets_table(metadata)
    metadata.create_all(engine)

    def make(**options: Any) -> SQLBudgetLimits:
        return SQLBudgetLimits(sessionmaker(engine), table, "llm", **options)

    yield make
    engine.dispose()


class TestSQLBudgetLimits:
    async def test_the_most_specific_row(self, store: Callable[..., SQLBudgetLimits]) -> None:
        plans = {"ana": "pro", "ben": "pro"}
        limits = store(plan=plans.get)
        assert await limits("ana") is None
        await limits.save(BudgetLimits(quota=100), scope="default")
        assert (await limits("cy")).quota == 100  # type: ignore[union-attr]
        await limits.save(BudgetLimits(quota=5_000, rate=60), plan="pro")
        assert (await limits("ana")) == BudgetLimits(quota=5_000, rate=60)
        await limits.save(BudgetLimits(quota=0, enabled=False), user="ben")
        assert (await limits("ben")) == BudgetLimits(quota=0, enabled=False)
        await limits.save(BudgetLimits(quota=1, period="day"), user="ben")  # replaced
        assert (await limits("ben")).period == "day"  # type: ignore[union-attr]
        assert await limits.delete(user="ben")
        assert not await limits.delete(user="ben")
        assert (await limits("ben")).quota == 5_000  # type: ignore[union-attr]
        assert await limits.total() is None
        await limits.save(BudgetLimits(quota=1_000_000), scope="total")
        assert (await limits.total()).quota == 1_000_000  # type: ignore[union-attr]

    async def test_with_the_budget(self, store: Callable[..., SQLBudgetLimits]) -> None:
        limits = store()
        await limits.save(BudgetLimits(quota=200), scope="default")
        await limits.save(BudgetLimits(quota=250), scope="total")
        setup = Setup(limits, total=limits.total)
        assert setup.post(used=200).json()["used"] == 200
        assert setup.post("ben", used=200).json()["reserved"] == 50

    async def test_async_sessions(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        metadata = MetaData()
        table = provider_budgets_table(metadata)
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        async def plan_of(name: str) -> str | None:
            return "free" if name else None

        limits = SQLBudgetLimits(async_sessionmaker(engine), table, "llm", plan=plan_of)
        await limits.save(BudgetLimits(quota=7), plan="free")
        assert (await limits("ana")).quota == 7  # type: ignore[union-attr]
        await engine.dispose()

    @pytest.mark.parametrize(
        "names",
        [{}, {"user": "a", "plan": "b"}, {"scope": "everyone"}],
    )
    async def test_one_scope(
        self,
        names: dict[str, str],
        store: Callable[..., SQLBudgetLimits],
    ) -> None:
        with pytest.raises(ValueError, match="one of user|default"):
            await store().save(BudgetLimits(), **names)


class TestEdges:
    async def test_the_call_and_its_remaining(self) -> None:
        setup = Setup()
        async with setup.budget.call("ana", 100, cache=setup.cache) as call:
            call.use(30)
            assert call.remaining == 70
            with pytest.raises(ValueError, match="whole number"):
                call.use(-1)
        await setup.budget.aclose()

    async def test_few_users_kept(self) -> None:
        reads: list[str] = []

        def limits_of(name: str) -> BudgetLimits:
            reads.append(name)
            return BudgetLimits()

        setup = Setup(limits_of, cache_size=1)
        for name in ("ana", "ben", "ana"):
            setup.post(name)
        assert reads == ["ana", "ben", "ana"]  # ana was dropped for ben

    def test_the_apps_cap_per_call(self) -> None:
        setup = Setup(BudgetLimits(), total=BudgetLimits(max_per_call=60))
        assert setup.post().json() == {"reserved": 60, "used": 60}

    async def test_giving_back_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        class GiveBackFails(InMemoryBackend):
            async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
                if amount < 0:
                    raise ConnectionError("down")
                return await super().incr(key, amount, expire)

        setup = Setup(backend=GiveBackFails())
        setup.post(used=100)
        assert await setup.used() == 300  # what wasn't used stays spent: never past the quota
        assert "Couldn't give back 200 units of llm" in caplog.text

    async def test_a_sink_that_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        class Broken:
            async def write(self, record: Any) -> None:
                raise OSError("disk full")

        setup = Setup(sinks=[Broken()])
        setup.sink = MemoryUsageSink()
        assert setup.post(used=10).status_code == 200
        assert "Usage record not written by Broken (OSError)" in caplog.text
        assert '"amount":10' in caplog.text.replace(" ", "")
