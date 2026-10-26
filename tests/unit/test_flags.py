"""FastAPIFeatureFlags: flags in the backend, sticky percentage rollouts, local copies."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import json
import logging
from typing import Any

import fakeredis
import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import BackendUnavailable, Cache, FastAPICache
from fastapi_alcazaba import flags as flags_module
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.flags import MAX_SUBJECTS, FastAPIFeatureFlags, FeatureFlag
from fastapi_alcazaba.types import Backend

DIGEST = "0" * 64


@pytest.fixture(name="cache", params=["memory", "redis"])
def fixture_cache(request: pytest.FixtureRequest) -> Cache:
    backend: Backend = (
        InMemoryBackend()
        if request.param == "memory"
        else RedisBackend(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer()))
    )
    return Cache(backend, prefix="app")


class Counting(InMemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.gets = 0
        self.failing = False
        self.gate: asyncio.Event | None = None

    async def get(self, key: str) -> bytes | None:
        self.gets += 1
        if self.failing:
            raise ConnectionError("down")
        value = await super().get(key)
        if self.gate is not None:
            await self.gate.wait()
        return value


def by_header(connection: HTTPConnection) -> str | None:
    return connection.headers.get("x-user")


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestEvaluation:
    async def test_undefined_flags_give_the_default(self, cache: Cache) -> None:
        flags = FastAPIFeatureFlags(cache)
        assert not await flags.enabled("new-checkout")
        assert await flags.enabled("new-checkout", default=True)
        assert await flags.get("new-checkout") is None

    async def test_on_and_off_for_everybody(self, cache: Cache) -> None:
        flags = FastAPIFeatureFlags(cache)
        await flags.set("new-checkout", enabled=True)
        assert await flags.enabled("new-checkout")
        assert await flags.enabled("new-checkout", subject="user-1")
        await flags.set("new-checkout", enabled=False, allow=["user-1"])
        assert not await flags.enabled("new-checkout", default=True)
        assert not await flags.enabled("new-checkout", subject="user-1")  # off beats allow

    async def test_allow_and_deny(self, cache: Cache) -> None:
        flags = FastAPIFeatureFlags(cache)
        await flags.set(
            "beta", enabled=True, percentage=0, allow=["alice", "bob"], deny=["bob", "carol"]
        )
        assert await flags.enabled("beta", "alice")
        assert not await flags.enabled("beta", "bob")  # deny beats allow
        assert not await flags.enabled("beta", "carol")
        assert not await flags.enabled("beta", "dave")
        await flags.set("beta", enabled=True, deny=["carol"])
        assert await flags.enabled("beta", "dave")
        assert not await flags.enabled("beta", "carol")

    async def test_an_allow_list_without_a_percentage_is_only_for_it(self, cache: Cache) -> None:
        flags = FastAPIFeatureFlags(cache)
        flag = await flags.set("beta", enabled=True, allow=["alice"])
        assert (flag.percentage, flag.share) == (None, 0.0)
        assert await flags.enabled("beta", subject="alice")
        assert not await flags.enabled("beta", subject="bob")
        assert not await flags.enabled("beta")  # nobody in particular
        everybody = await flags.set("beta", enabled=True, deny=["bob"])
        assert everybody.share == 100.0
        assert await flags.enabled("beta", subject="carol")
        assert not await flags.enabled("beta", subject="bob")

    async def test_partial_rollouts_need_a_subject(self, cache: Cache) -> None:
        flags = FastAPIFeatureFlags(cache)
        await flags.set("beta", enabled=True, percentage=99.9)
        assert not await flags.enabled("beta", default=True)
        await flags.set("beta", enabled=True, percentage=100)
        assert await flags.enabled("beta")

    def test_percentages_are_sticky_and_grow(self) -> None:
        subjects = [f"user-{n}" for n in range(10_000)]

        def rollout(percentage: float) -> set[str]:
            flag = FeatureFlag("checkout", True, percentage)
            return {subject for subject in subjects if flag.is_on(subject)}

        ten, twenty = rollout(10), rollout(20)
        assert 850 < len(ten) < 1150
        assert 1800 < len(twenty) < 2200
        assert ten < twenty  # raising it only adds subjects
        assert rollout(10) == ten
        assert rollout(0) == set()
        assert rollout(100) == set(subjects)
        # Another flag, other subjects.
        other = FeatureFlag("search", True, 10)
        assert {subject for subject in subjects if other.is_on(subject)} != ten

    async def test_subjects_are_hashed(self) -> None:
        backend = InMemoryBackend()
        flags = FastAPIFeatureFlags(Cache(backend, prefix="app"))
        flag = await flags.set("beta", enabled=True, allow=["alice@example.com"], deny=["bob"])
        stored = await backend.get("app!flags:beta")
        assert stored is not None
        assert b"example" not in stored
        assert b"bob" not in stored
        assert "alice@example.com" not in flag.allow
        # Salted with the flag's name: the same subject has another digest in another flag.
        other = await flags.set("gamma", enabled=True, allow=["alice@example.com"])
        assert not flag.allow & other.allow


class TestRequire:
    async def test_404_when_off(self, cache: Cache) -> None:
        flags = FastAPIFeatureFlags(cache)
        app = FastAPI()

        @app.get("/beta", dependencies=[Depends(flags.require("beta"))])
        async def beta() -> dict[str, bool]:
            return {"ok": True}

        async with client(app) as http:
            hidden = await http.get("/beta")
            assert hidden.status_code == 404
            assert hidden.json() == (await http.get("/missing")).json() == {"detail": "Not Found"}
            await flags.set("beta", enabled=True)
            assert (await http.get("/beta")).json() == {"ok": True}

    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_by_subject(self, cache: Cache, asynchronous: bool) -> None:
        async def by_header_async(connection: HTTPConnection) -> str | None:
            return by_header(connection)

        flags = FastAPIFeatureFlags(cache)
        await flags.set("beta", enabled=True, percentage=0, allow=["alice"])
        subject = by_header_async if asynchronous else by_header
        app = FastAPI()
        app.get("/beta", dependencies=[Depends(flags.require("beta", subject=subject))])(
            lambda: None
        )
        async with client(app) as http:
            assert (await http.get("/beta", headers={"x-user": "alice"})).status_code == 200
            assert (await http.get("/beta", headers={"x-user": "bob"})).status_code == 404
            assert (await http.get("/beta")).status_code == 404

    async def test_default(self, cache: Cache) -> None:
        flags = FastAPIFeatureFlags(cache)
        app = FastAPI()
        app.get("/beta", dependencies=[Depends(flags.require("beta", default=True))])(lambda: None)
        async with client(app) as http:
            assert (await http.get("/beta")).status_code == 200

    def test_websockets_are_closed_with_policy_violation(self) -> None:
        flags = FastAPIFeatureFlags(Cache(InMemoryBackend()))
        app = FastAPI()

        @app.websocket("/ws", dependencies=[Depends(flags.require("live"))])
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()  # pragma: no cover  # refused before

        with (
            pytest.raises(WebSocketDisconnect) as refused,
            TestClient(app).websocket_connect("/ws"),
        ):
            pass
        assert refused.value.code == 1008


class TestLocalCopies:
    async def test_definitions_are_kept_for_refresh_seconds(self) -> None:
        backend = Counting()
        flags = FastAPIFeatureFlags(Cache(backend), refresh=60)
        await flags.set("beta", enabled=True)
        for _ in range(10):
            assert await flags.enabled("beta")
        assert backend.gets == 0  # set() left a copy
        for _ in range(10):
            assert not await flags.enabled("other")
        assert backend.gets == 1

    async def test_changes_reach_the_other_processes(self, cache: Cache) -> None:
        here = FastAPIFeatureFlags(cache)
        elsewhere = FastAPIFeatureFlags(cache, refresh=0.1)
        assert not await elsewhere.enabled("beta")
        await here.set("beta", enabled=True)
        assert not await elsewhere.enabled("beta")  # its copy is still fresh
        await asyncio.sleep(0.12)
        assert await elsewhere.enabled("beta")
        await here.delete("beta")
        assert not await here.enabled("beta", default=False)
        await asyncio.sleep(0.12)
        assert await elsewhere.enabled("beta", default=True)

    async def test_a_new_fastapi_cache_drops_the_copies(self) -> None:
        """As each test's lifespan sets FastAPICache up again: the last app's flags aren't used."""
        flags = FastAPIFeatureFlags(refresh=60)
        FastAPICache.init(InMemoryBackend())
        await flags.set("beta", enabled=True)
        assert await flags.enabled("beta")
        FastAPICache.reset()
        FastAPICache.init(InMemoryBackend())
        assert not await flags.enabled("beta")

    async def test_forget_drops_the_copies(self) -> None:
        backend = Counting()
        flags = FastAPIFeatureFlags(Cache(backend), refresh=60)
        await flags.enabled("beta")
        await flags.enabled("beta")
        assert backend.gets == 1
        flags.forget()
        await flags.enabled("beta")
        assert backend.gets == 2

    async def test_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(flags_module, "MAX_KNOWN_FLAGS", 2)
        flags = FastAPIFeatureFlags(Cache(InMemoryBackend()))
        for name in ("a", "b", "a", "c"):
            await flags.enabled(name)
        assert list(flags._known) == ["a", "c"]

    async def test_a_read_that_started_before_a_change_is_stale(self) -> None:
        backend = Counting()
        flags = FastAPIFeatureFlags(Cache(backend))
        gate = backend.gate = asyncio.Event()
        reading = asyncio.create_task(flags.get("beta"))
        await asyncio.sleep(0.01)  # it read nothing, and waits
        backend.gate = None
        await flags.set("beta", enabled=True)
        gate.set()
        assert await reading is None  # what the backend had when it was read
        assert await flags.enabled("beta")  # the copy keeps the newer change

    async def test_in_the_shared_backend(self) -> None:
        remote = InMemoryBackend()
        await FastAPIFeatureFlags(Cache(TieredBackend(remote), prefix="app")).set(
            "beta", enabled=True
        )
        assert await remote.get("app!flags:beta") is not None

    async def test_through_fastapi_cache(self) -> None:
        FastAPICache.init(InMemoryBackend())
        await FastAPIFeatureFlags().set("beta", enabled=True)
        assert await FastAPIFeatureFlags().enabled("beta")


class TestBackendFailures:
    async def test_the_last_definition_read_stays(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Counting()
        flags = FastAPIFeatureFlags(Cache(backend, circuit_breaker_failures=None), refresh=0.05)
        await flags.set("beta", enabled=True)
        backend.failing = True
        await asyncio.sleep(0.06)
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.flags"):
            assert await flags.enabled("beta")
            gets = backend.gets
            assert await flags.enabled("beta")  # not asked again before `refresh`
            assert backend.gets == gets
        assert "'beta' not refreshed: the backend failed" in caplog.text
        with pytest.raises(BackendUnavailable):
            await flags.get("beta")

    async def test_never_read_gives_the_default(self) -> None:
        backend = Counting()
        backend.failing = True
        flags = FastAPIFeatureFlags(Cache(backend, circuit_breaker_failures=None))
        assert await flags.enabled("beta", default=True)
        assert not await flags.enabled("beta")

    async def test_an_open_circuit_is_only_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = Counting()
        backend.failing = True
        flags = FastAPIFeatureFlags(Cache(backend, circuit_breaker_failures=1))
        with caplog.at_level(logging.DEBUG, "fastapi_alcazaba.flags"):
            await flags.enabled("a")
            await flags.enabled("b")
        assert "circuit breaker is open" in caplog.text

    async def test_writes_raise(self) -> None:
        class Down(InMemoryBackend):
            async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
                raise ConnectionError("down")

            async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
                raise ConnectionError("down")

        flags = FastAPIFeatureFlags(Cache(Down()))
        with pytest.raises(BackendUnavailable):
            await flags.set("beta", enabled=True)
        with pytest.raises(BackendUnavailable):
            await flags.delete("beta")


class TestStoredDefinitions:
    async def test_planted_definitions_are_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = InMemoryBackend()
        flags = FastAPIFeatureFlags(Cache(backend, prefix="app", signing_key="k" * 32))
        await flags.set("beta", enabled=True)
        assert await flags.get("beta") is not None
        planted: dict[str, Any] = {"enabled": True, "percentage": None, "allow": [], "deny": []}
        await backend.set("app!flags:planted", json.dumps(planted).encode())
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.flags"):
            assert await flags.get("planted") is None
        assert "didn't write" in caplog.text

    @pytest.mark.parametrize(
        "definition",
        [
            "not json",
            [1, 2],
            {"enabled": True, "percentage": None, "allow": []},
            {"enabled": "yes", "percentage": None, "allow": [], "deny": []},
            {"enabled": True, "percentage": "5", "allow": [], "deny": []},
            {"enabled": True, "percentage": True, "allow": [], "deny": []},
            {"enabled": True, "percentage": 101, "allow": [], "deny": []},
            {"enabled": True, "percentage": None, "allow": "x", "deny": []},
            {"enabled": True, "percentage": None, "allow": [], "deny": ["alice"]},
            {"enabled": True, "percentage": None, "allow": [DIGEST] * (MAX_SUBJECTS + 1)},
        ],
    )
    async def test_malformed_definitions_are_ignored(
        self,
        definition: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        backend = InMemoryBackend()
        payload = definition if isinstance(definition, str) else json.dumps(definition)
        await backend.set("app!flags:beta", payload.encode())
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.flags"):
            assert await FastAPIFeatureFlags(Cache(backend, prefix="app")).get("beta") is None
        assert "malformed" in caplog.text

    async def test_definitions_written_by_others(self) -> None:
        backend = InMemoryBackend()
        definition = {"enabled": True, "percentage": 50, "allow": [DIGEST], "deny": []}
        await backend.set("app!flags:beta", json.dumps(definition).encode())
        flag = await FastAPIFeatureFlags(Cache(backend, prefix="app")).get("beta")
        assert flag == FeatureFlag("beta", True, 50.0, frozenset([DIGEST]))


class TestValidation:
    @pytest.mark.parametrize(
        "name", ["", "-beta", "beta flag", "beta{x}", "a" * 101, "bêta", "beta\n"]
    )
    async def test_names(self, name: str) -> None:
        flags = FastAPIFeatureFlags(Cache(InMemoryBackend()))
        with pytest.raises(ValueError, match="invalid flag name"):
            await flags.enabled(name)
        with pytest.raises(ValueError, match="invalid flag name"):
            flags.require(name)

    async def test_valid_names(self) -> None:
        flags = FastAPIFeatureFlags(Cache(InMemoryBackend()))
        for name in ("beta", "Beta_2", "checkout.v2", "a-b", "a" * 100):
            await flags.set(name, enabled=True)
            assert await flags.enabled(name)

    async def test_definitions(self) -> None:
        flags = FastAPIFeatureFlags(Cache(InMemoryBackend()))
        with pytest.raises(ValueError, match="percentage"):
            await flags.set("beta", enabled=True, percentage=101)
        with pytest.raises(ValueError, match="percentage"):
            await flags.set("beta", enabled=True, percentage=float("nan"))
        with pytest.raises(TypeError, match="enabled must be True or False"):
            await flags.set("beta", enabled="false")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="not a single string"):
            await flags.set("beta", enabled=True, allow="alice")
        with pytest.raises(TypeError, match="subjects are strings, not int"):
            await flags.set("beta", enabled=True, deny=[42])  # type: ignore[list-item]
        with pytest.raises(ValueError, match="more than"):
            await flags.set("beta", enabled=True, allow=(str(n) for n in range(MAX_SUBJECTS + 1)))
        await flags.set("beta", enabled=True, allow=["alice"] * (MAX_SUBJECTS + 1))  # unique: 1
        assert await flags.get("beta") is not None

    def test_arguments(self) -> None:
        with pytest.raises(ValueError, match="refresh"):
            FastAPIFeatureFlags(refresh=0)
        with pytest.raises(ValueError, match="refresh"):
            FastAPIFeatureFlags(refresh=float("inf"))
