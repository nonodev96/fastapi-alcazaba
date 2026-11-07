"""UserSessions: each user's server sessions, listed ("your devices") and ended, one or all of
them, across the browsers they logged in from."""

# pylint: disable=protected-access  # the index, where some tests make it miss a session

import asyncio
import json
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from fastapi_alcazaba import Cache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import (
    ServerSessionMiddleware,
    UserSessions,
    clear_session,
    rotate_session,
)
from fastapi_alcazaba.security import sessions as sessions_module

KEY = "session-signing-key-of-32-bytes!!"  # the tests' own


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture(name="clock", autouse=True)
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(sessions_module, "time", clock)
    return clock


class Site:
    """An app with UserSessions, and a browser (a TestClient) per device."""

    def __init__(self, backend: InMemoryBackend | None = None, **options: Any) -> None:
        self.backend = backend or InMemoryBackend()
        self.cache = Cache(self.backend, signing_key=KEY)
        self.users = UserSessions(**options)
        app = FastAPI()
        users = self.users

        @app.post("/login/{user}")
        async def login(request: Request, user: str) -> None:
            request.session["user_id"] = user
            rotate_session(request)

        @app.post("/switch/{user}")
        async def switch(request: Request, user: str) -> None:
            request.session["user_id"] = user  # another user, same session (no rotation)

        @app.post("/logout")
        async def logout(request: Request) -> None:
            clear_session(request)

        @app.get("/me")
        async def me(request: Request) -> str:
            if "user_id" not in request.session:
                raise HTTPException(401)
            return str(request.session["user_id"])

        @app.get("/sessions")
        async def sessions(request: Request) -> list[dict[str, Any]]:
            found = await users.list(request.session["user_id"], request=request)
            return [
                {"ref": s.ref, "current": s.current, "agent": s.user_agent, "client": s.client}
                for s in found
            ]

        @app.delete("/sessions/{ref}")
        async def end(request: Request, ref: str) -> bool:
            return await users.end(request.session["user_id"], ref, request=request)

        @app.post("/everywhere")
        async def everywhere(request: Request, keep: bool = True) -> int:
            user = request.session["user_id"]
            return await users.end_all(user, request=request, keep_current=keep)

        self.cache.install(app)
        app.add_middleware(ServerSessionMiddleware, users=users, cookie_name="s", secure=False)
        self.app = app

    def device(self, name: str) -> TestClient:
        browser = TestClient(self.app)
        browser.headers["User-Agent"] = name  # instead of TestClient's own
        return browser

    def logged_in(self, user: str, *devices: str) -> list[TestClient]:
        browsers = [self.device(name) for name in devices]
        for browser in browsers:
            assert browser.post(f"/login/{user}").status_code == 200
        return browsers

    def index(self, user: str) -> dict[str, Any]:
        """The user's index, as the backend holds it (sealed)."""
        key = self.users._key(self.cache, "user", user)
        stored = asyncio.run(self.backend.get(key))
        if stored is None:
            return {}
        protector = self.cache.options.protector
        assert protector is not None
        payload = protector.open(key, stored)
        assert payload is not None
        index: dict[str, Any] = json.loads(payload)
        return index

    def forget(self, user: str) -> None:
        """The index loses the user's sessions: a backend that lost it, a race without locks."""
        asyncio.run(self.backend.clear(key=self.users._key(self.cache, "user", user)))

    def expire(self, user: str, agent: str) -> None:
        """The backend drops the session of `agent`, as when it expires."""
        key = next(e["key"] for e in self.index(user).values() if e.get("agent") == agent)
        asyncio.run(self.backend.clear(key=key))


def logged_in(browser: TestClient) -> bool:
    return browser.get("/me").status_code == 200


def agents(browser: TestClient) -> list[str]:
    return sorted(session["agent"] for session in browser.get("/sessions").json())


class TestListing:
    def test_a_users_sessions(self) -> None:
        site = Site()
        laptop, phone, _ = site.logged_in("ana", "laptop", "phone", "tablet")
        site.logged_in("ben", "desktop")
        found = laptop.get("/sessions").json()
        assert sorted(s["agent"] for s in found) == ["laptop", "phone", "tablet"]
        assert [s["agent"] for s in found if s["current"]] == ["laptop"]
        assert {s["client"] for s in found} == {"testclient"}
        assert [s["agent"] for s in phone.get("/sessions").json() if s["current"]] == ["phone"]

    def test_newest_first(self, clock: Clock) -> None:
        site = Site()
        (laptop,) = site.logged_in("ana", "laptop")
        clock.now += 60
        site.logged_in("ana", "phone")
        assert [s["agent"] for s in laptop.get("/sessions").json()] == ["phone", "laptop"]

    def test_without_the_client(self) -> None:
        site = Site(record_client=False)
        (laptop,) = site.logged_in("ana", "laptop")
        assert laptop.get("/sessions").json()[0]["agent"] is None

    def test_sealed_like_the_sessions(self) -> None:
        """Signed with the cache's key: an index changed in the backend is read as empty."""
        site = Site()
        (laptop,) = site.logged_in("ana", "a-browser-of-ana")
        key = site.users._key(site.cache, "user", "ana")
        stored = asyncio.run(site.backend.get(key))
        assert stored is not None
        tampered = stored.replace(b"a-browser-of-ana", b"a-browser-of-eve")
        asyncio.run(site.backend.set(key, tampered, 60))
        assert laptop.get("/sessions").json() == []

    def test_rotations_and_logouts_keep_it(self) -> None:
        site = Site()
        laptop, phone = site.logged_in("ana", "laptop", "phone")
        laptop.post("/login/ana")  # a new id: the same device
        assert agents(laptop) == ["laptop", "phone"]
        phone.post("/logout")
        assert agents(laptop) == ["laptop"]

    def test_another_user_in_the_same_session(self) -> None:
        site = Site()
        (laptop,) = site.logged_in("ana", "laptop")
        (desk,) = site.logged_in("ben", "desk")
        laptop.post("/switch/ben")
        assert agents(desk) == ["desk", "laptop"]
        assert site.index("ana") == {}

    def test_sessions_the_backend_dropped_are_dropped(self) -> None:
        site = Site()
        (laptop,) = site.logged_in("ana", "laptop", "phone")[:1]
        site.expire("ana", "phone")
        assert agents(laptop) == ["laptop"]
        assert len(site.index("ana")) == 1

    def test_at_most_max_sessions(self, clock: Clock) -> None:
        site = Site(max_sessions=2)
        first = site.logged_in("ana", "first")[0]
        clock.now += 1
        second = site.logged_in("ana", "second")[0]
        clock.now += 1
        site.logged_in("ana", "third")
        assert not logged_in(first)  # the oldest ended
        assert agents(second) == ["second", "third"]


class TestEnding:
    def test_one_session(self) -> None:
        site = Site()
        laptop, phone = site.logged_in("ana", "laptop", "phone")
        ref = next(s["ref"] for s in laptop.get("/sessions").json() if s["agent"] == "phone")
        assert laptop.delete(f"/sessions/{ref}").json() is True
        assert not logged_in(phone)
        assert logged_in(laptop)
        assert agents(laptop) == ["laptop"]

    def test_only_the_users_own(self) -> None:
        site = Site()
        (laptop,) = site.logged_in("ana", "laptop")
        (desk,) = site.logged_in("ben", "desk")
        ref = desk.get("/sessions").json()[0]["ref"]
        assert laptop.delete(f"/sessions/{ref}").json() is False
        assert logged_in(desk)

    def test_its_own(self) -> None:
        site = Site()
        (laptop,) = site.logged_in("ana", "laptop")
        ref = laptop.get("/sessions").json()[0]["ref"]
        assert laptop.delete(f"/sessions/{ref}").json() is True
        assert not logged_in(laptop)

    def test_everywhere_but_here(self, clock: Clock) -> None:
        site = Site()
        laptop, phone, tablet = site.logged_in("ana", "laptop", "phone", "tablet")
        (desk,) = site.logged_in("ben", "desk")
        clock.now += 10
        assert laptop.post("/everywhere").json() == 2
        assert [logged_in(b) for b in (laptop, phone, tablet, desk)] == [True, False, False, True]
        assert agents(laptop) == ["laptop"]

    def test_everywhere(self, clock: Clock) -> None:
        site = Site()
        laptop, phone = site.logged_in("ana", "laptop", "phone")
        clock.now += 10
        assert laptop.post("/everywhere?keep=false").json() == 2
        assert not logged_in(laptop)
        assert not logged_in(phone)
        (again,) = site.logged_in("ana", "laptop")  # a login after it works
        assert logged_in(again)

    def test_everywhere_even_what_the_index_missed(self, clock: Clock) -> None:
        site = Site()
        laptop, phone = site.logged_in("ana", "laptop", "phone")
        site.forget("ana")
        clock.now += 10
        assert laptop.post("/login/ana").status_code == 200  # indexed again, the phone isn't
        assert agents(laptop) == ["laptop"]
        clock.now += 10
        assert laptop.post("/everywhere").json() == 0  # none in the index but this one
        assert not logged_in(phone)  # refused all the same: it started before
        assert logged_in(laptop)

    @pytest.mark.parametrize("missed", [False, True])
    async def test_a_rotation_still_running_doesnt_outlive_everywhere(
        self,
        clock: Clock,
        missed: bool,
    ) -> None:
        """A copied cookie in a slow request that rotates the session (a login, a step-up) while
        another device ends the user's sessions: it gets no new one, even when the index missed
        it; and the device that ended them keeps its own."""
        site = Site()
        started, release = asyncio.Event(), asyncio.Event()

        @site.app.post("/slow-login")
        async def slow_login(request: Request) -> None:
            started.set()
            await release.wait()
            rotate_session(request)

        transport = httpx.ASGITransport(app=site.app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as phone,
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as laptop,
        ):
            await phone.post("/login/ana")
            copied = {"Cookie": f"s={phone.cookies['s']}"}
            if missed:
                await site.backend.clear(key=site.users._key(site.cache, "user", "ana"))
            await laptop.post("/login/ana")
            clock.now += 10
            slow = asyncio.create_task(phone.post("/slow-login", headers=copied))
            await started.wait()
            assert (await laptop.post("/everywhere")).status_code == 200
            release.set()
            response = await slow
            assert response.status_code == 200
            assert "Max-Age=0" in response.headers["set-cookie"]  # expired, not a new id
            assert (await phone.get("/me", headers=copied)).status_code == 401
            assert (await laptop.get("/me")).json() == "ana"

    def test_a_failing_backend_refuses_the_users_sessions(self) -> None:
        class NoCutoffs(InMemoryBackend):
            fail = False

            async def get(self, key: str) -> bytes | None:
                if self.fail and ":ended:" in key:
                    raise ConnectionError("down")
                return await super().get(key)

        backend = NoCutoffs()
        site = Site(backend)
        (laptop,) = site.logged_in("ana", "laptop")
        backend.fail = True
        assert not logged_in(laptop)  # fail closed: it can't tell whether it was ended
        anonymous = site.device("anonymous")
        assert anonymous.get("/me").status_code == 401


class TestConfiguration:
    async def test_not_given_to_a_middleware(self) -> None:
        with pytest.raises(RuntimeError, match="ServerSessionMiddleware"):
            await UserSessions().list("ana")

    def test_one_middleware(self) -> None:
        users = UserSessions()
        users.bind("session", 60)
        users.bind("session", 60)
        with pytest.raises(ValueError, match="one ServerSessionMiddleware"):
            users.bind("other", 60)

    def test_max_sessions(self) -> None:
        with pytest.raises(ValueError, match="max_sessions"):
            UserSessions(max_sessions=0)

    def test_another_user_key(self) -> None:
        site = Site(user_key="account")
        (laptop,) = site.logged_in("ana", "laptop")
        assert site.index("ana") == {}  # its sessions have user_id, not account
        assert logged_in(laptop)
