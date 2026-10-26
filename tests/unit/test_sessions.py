"""ServerSessionMiddleware: the session lives in the backend, the cookie only carries its id."""

# pylint: disable=protected-access  # tests look at the internals they check

import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from fastapi_alcazaba import Backend, Cache, FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.sessions import (
    ServerSession,
    ServerSessionMiddleware,
    clear_session,
    rotate_session,
)

SIGNING_KEY = "s" * 32
COOKIE = re.compile(
    r"session=([A-Za-z0-9_-]{43}); Max-Age=(\d+); Path=/; HttpOnly; Secure; SameSite=Lax"
)
EXPIRED = (
    "session=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/; HttpOnly; Secure; "
    "SameSite=Lax"
)
UNKNOWN_ID = "u" * 43


class Clock:
    """Stands in for the time module in fastapi_alcazaba.sessions."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


class Spy(InMemoryBackend):
    """Counts calls, and fails the operations in `failing`."""

    def __init__(self) -> None:
        super().__init__()
        self.failing: set[str] = set()
        self.calls: list[str] = []

    def _call(self, operation: str) -> None:
        self.calls.append(operation)
        if operation in self.failing:
            raise ConnectionError("down")

    async def get(self, key: str) -> bytes | None:
        self._call("get")
        return await super().get(key)

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        self._call("set")
        await super().set(key, value, expire)

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        self._call("delete")
        return await super().clear(namespace, key)

    def sessions(self) -> dict[str, bytes]:
        return {key: value.data for key, value in self._state.items() if "!session:" in key}


class App:
    def __init__(
        self,
        backend: Backend | None = None,
        cache_options: dict[str, Any] | None = None,
        install: bool = True,
        **options: Any,
    ) -> None:
        self.backend = backend if isinstance(backend, Spy) else Spy()
        self.app = FastAPI()
        self.cache = Cache(backend or self.backend, prefix="app", **(cache_options or {}))
        if install:
            self.cache.install(self.app)
        self.app.add_middleware(ServerSessionMiddleware, **options)
        app = self.app

        @app.get("/session")
        async def read(request: Request) -> dict[str, Any]:
            return dict(request.session)

        @app.get("/public")
        async def public() -> str:
            return "public"

        @app.get("/user")
        async def user(request: Request) -> str | None:
            # As with Starlette before 1.0, whose request.session doesn't call mark_accessed.
            session: dict[str, Any] = request.scope["session"]
            return session.get("user")

        @app.post("/write-only")
        async def write_only(request: Request) -> None:
            request.scope["session"]["user"] = "alice"

        @app.post("/set")
        async def set_value(request: Request, key: str, value: str) -> None:
            request.session[key] = value

        @app.post("/cart")
        async def add(request: Request, item: str) -> None:
            request.session.setdefault("cart", []).append(item)

        @app.post("/login")
        async def login(request: Request) -> None:
            request.session["user"] = "alice"
            rotate_session(request)

        @app.post("/rotate")
        async def rotate(request: Request) -> None:
            rotate_session(request)

        @app.post("/logout")
        async def logout(request: Request, flash: str | None = None) -> None:
            clear_session(request)
            if flash is not None:
                request.session["flash"] = flash

        @app.post("/empty")
        async def empty(request: Request) -> None:
            request.session.clear()

        @app.post("/private")
        async def private(request: Request, response: Response) -> None:
            request.session["user"] = "alice"
            response.headers["Cache-Control"] = "private"
            response.headers["Vary"] = "Accept, Cookie"

        @app.post("/stream")
        async def stream(request: Request) -> StreamingResponse:
            request.session["streamed"] = True

            async def chunks() -> AsyncIterator[bytes]:
                for chunk in (b"a", b"b"):
                    yield chunk

            return StreamingResponse(chunks())

        @app.post("/big")
        async def big(request: Request, size: int) -> None:
            request.session["x"] = "a" * size

        @app.post("/not-json")
        async def not_json(request: Request) -> None:
            request.session["x"] = {1, 2}

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json(dict(websocket.session))
            websocket.session["changed"] = True
            await websocket.close()

        @app.websocket("/ws/rotate")
        async def ws_rotate(websocket: WebSocket) -> None:
            await websocket.accept()
            try:
                rotate_session(websocket)
            except RuntimeError as exc:
                await websocket.send_text(str(exc))
            await websocket.close()

    def client(self, **kwargs: Any) -> TestClient:
        return TestClient(self.app, base_url="https://testserver", **kwargs)

    def key(self, session_id: str) -> str:
        return self.cache.state_key("session", hashlib.sha256(session_id.encode()).hexdigest())


def issued(response: Any) -> str:
    match = COOKIE.fullmatch(response.headers["set-cookie"])
    assert match is not None, response.headers.get("set-cookie")
    return match.group(1)


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("fastapi_alcazaba.sessions.time", clock)
    return clock


class TestSessions:
    def test_a_session_is_created_when_data_is_stored(self) -> None:
        app = App(cache_options={"signing_key": SIGNING_KEY})
        client = app.client()
        response = client.post("/set?key=user&value=alice")
        session_id = issued(response)
        assert "; Max-Age=1209600;" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "Cookie"
        assert client.get("/session").json() == {"user": "alice"}
        assert list(app.backend.sessions()) == [app.key(session_id)]

    def test_empty_sessions_arent_stored(self) -> None:
        app = App()
        response = app.client().get("/session")
        assert response.json() == {}
        assert "set-cookie" not in response.headers
        assert response.headers["vary"] == "Cookie"
        assert not app.backend.sessions()

    def test_routes_that_dont_use_it(self) -> None:
        response = App().client().get("/public")
        assert "vary" not in response.headers
        assert "set-cookie" not in response.headers

    def test_reading_it_varies_the_response_without_request_session(self) -> None:
        client = App().client()
        response = client.post("/write-only")
        issued(response)
        assert "vary" not in response.headers  # written, not read: saving it doesn't count
        response = client.get("/user")
        assert response.json() == "alice"
        assert response.headers["vary"] == "Cookie"

    def test_every_read_marks_it(self) -> None:
        reads: list[Callable[[ServerSession], object]] = [
            lambda s: "a" in s,
            lambda s: s["a"],
            list,
            len,
            lambda s: s.get("a"),
            lambda s: s.items(),
            lambda s: s.keys(),
            lambda s: s.pop("a"),
            lambda s: s.setdefault("b", 2),
            lambda s: s.values(),
        ]
        for read in reads:
            session = ServerSession({"a": 1})
            read(session)
            assert session.accessed, read
        session = ServerSession({"a": 1})
        session["b"] = 2
        assert not session.accessed
        assert ServerSession.get.__name__ == "get"

    def test_saved_only_when_it_changed(self) -> None:
        app = App()
        client = app.client()
        client.post("/set?key=user&value=alice")
        app.backend.calls.clear()
        response = client.get("/session")
        assert "set-cookie" not in response.headers
        assert app.backend.calls == ["get"]
        client.post("/set?key=user&value=alice")
        assert app.backend.calls == ["get", "get"]  # the same value: no change
        client.post("/set?key=user&value=bob")
        assert app.backend.calls == ["get", "get", "get", "set"]
        assert client.get("/session").json() == {"user": "bob"}

    def test_nested_changes_are_saved(self) -> None:
        client = App().client()
        client.post("/cart?item=olive")
        client.post("/cart?item=oil")
        assert client.get("/session").json() == {"cart": ["olive", "oil"]}

    def test_keys_keep_their_order(self) -> None:
        client = App().client()
        for key in ("zeta", "alpha", "mu"):
            client.post(f"/set?key={key}&value=1")
        assert list(client.get("/session").json()) == ["zeta", "alpha", "mu"]

    def test_the_apps_cache_control_and_vary_are_kept(self) -> None:
        response = App().client().post("/private")
        issued(response)
        assert response.headers["cache-control"] == "private"
        assert response.headers.get_list("vary") == ["Accept, Cookie"]

    def test_the_backend_never_sees_the_id(self) -> None:
        app = App(cache_options={"encryption_key": "e" * 32})
        session_id = issued(app.client().post("/set?key=user&value=alice"))
        ((key, stored),) = app.backend.sessions().items()
        assert session_id not in key
        assert b"alice" not in stored

    def test_the_global_cache_by_default(self) -> None:
        backend = Spy()
        FastAPICache.init(backend, signing_key=SIGNING_KEY)
        client = App(install=False).client()
        client.post("/set?key=user&value=alice")
        assert backend.sessions()
        assert client.get("/session").json() == {"user": "alice"}

    def test_only_the_shared_tier_of_a_tiered_backend(self) -> None:
        remote = Spy()
        tiered = TieredBackend(remote, invalidation_channel=None)
        client = App(tiered).client()
        client.post("/set?key=user&value=alice")
        assert remote.sessions()
        assert not [key for key in tiered.local._state if "!session:" in key]
        assert client.get("/session").json() == {"user": "alice"}

    def test_lifespan_goes_through(self) -> None:
        with App().client() as client:
            assert client.get("/public").status_code == 200


class TestIds:
    def test_an_unknown_id_never_becomes_a_session(self) -> None:
        """Session fixation: an id the client made up is never adopted."""
        app = App()
        response = app.client().post(
            "/set?key=user&value=alice", headers={"Cookie": f"session={UNKNOWN_ID}"}
        )
        assert issued(response) != UNKNOWN_ID
        assert app.key(UNKNOWN_ID) not in app.backend.sessions()

    @pytest.mark.parametrize("value", ["short", "a" * 44, "a" * 42 + "!"])
    def test_malformed_ids_dont_reach_the_backend(self, value: str) -> None:
        app = App()
        response = app.client().get("/session", headers={"Cookie": f"session={value}"})
        assert response.json() == {}
        assert app.backend.calls == []

    def test_the_first_well_formed_id(self) -> None:
        app = App()
        client = app.client()
        session_id = issued(client.post("/set?key=user&value=alice"))
        client.cookies.clear()
        cookie = f"session=bad; session={session_id}; session={UNKNOWN_ID}"
        assert client.get("/session", headers={"Cookie": cookie}).json() == {"user": "alice"}

    def test_rotation_gives_a_new_id_with_the_same_data(self) -> None:
        app = App()
        client = app.client()
        before = issued(client.post("/set?key=cart&value=olive"))
        after = issued(client.post("/login"))
        assert after != before
        assert list(app.backend.sessions()) == [app.key(after)]
        assert client.get("/session").json() == {"cart": "olive", "user": "alice"}
        stale = client.get("/session", headers={"Cookie": f"session={before}"})
        assert stale.json() == {}

    def test_rotating_an_empty_session(self) -> None:
        app = App()
        response = app.client().post("/rotate", headers={"Cookie": f"session={UNKNOWN_ID}"})
        assert response.headers["set-cookie"] == EXPIRED
        assert "set-cookie" not in app.client().post("/rotate").headers


class TestClearing:
    def test_logging_out_deletes_the_session(self) -> None:
        app = App()
        client = app.client()
        session_id = issued(client.post("/set?key=user&value=alice"))
        response = client.post("/logout")
        assert response.headers["set-cookie"] == EXPIRED
        assert not app.backend.sessions()
        stolen = app.client().get("/session", headers={"Cookie": f"session={session_id}"})
        assert stolen.json() == {}

    def test_data_after_logging_out_goes_to_a_new_session(self) -> None:
        app = App()
        client = app.client()
        before = issued(client.post("/set?key=user&value=alice"))
        after = issued(client.post("/logout?flash=bye"))
        assert after != before
        assert client.get("/session").json() == {"flash": "bye"}
        assert list(app.backend.sessions()) == [app.key(after)]

    def test_a_session_the_app_empties(self) -> None:
        app = App()
        client = app.client()
        client.post("/set?key=user&value=alice")
        assert client.post("/empty").headers["set-cookie"] == EXPIRED
        assert not app.backend.sessions()

    def test_logging_out_without_a_session(self) -> None:
        assert "set-cookie" not in App().client().post("/logout").headers


class TestExpiry:
    def test_max_age(self, clock: Clock) -> None:
        app = App(max_age=3600)
        client = app.client()
        client.post("/set?key=user&value=alice")
        clock.now += 3599
        assert client.get("/session").json() == {"user": "alice"}
        clock.now += 1
        assert client.get("/session").json() == {}

    def test_rotation_starts_a_new_lifetime(self, clock: Clock) -> None:
        client = App(max_age=3600).client()
        client.post("/set?key=user&value=alice")
        clock.now += 3000
        client.post("/rotate")
        clock.now += 3000
        assert client.get("/session").json() == {"user": "alice"}

    def test_idle_timeout(self, clock: Clock) -> None:
        app = App(max_age=3600, idle_timeout=600)
        client = app.client()
        client.post("/set?key=user&value=alice")
        for _ in range(5):  # each request within the idle timeout keeps it alive
            clock.now += 599
            assert client.get("/session").json() == {"user": "alice"}
        clock.now += 600
        assert client.get("/session").json() == {}

    def test_the_idle_timeout_is_refreshed_now_and_then(self, clock: Clock) -> None:
        app = App(max_age=3600, idle_timeout=600)
        client = app.client()
        client.post("/set?key=user&value=alice")
        app.backend.calls.clear()
        clock.now += 59
        client.get("/session")
        assert app.backend.calls == ["get"]
        clock.now += 1
        client.get("/session")
        assert app.backend.calls == ["get", "get", "set"]
        ((key, stored),) = app.backend.sessions().items()
        assert json.loads(stored)["seen"] == int(clock.now)
        assert app.backend._state[key].expires_at is not None

    def test_a_failed_refresh_isnt_an_error(self, clock: Clock) -> None:
        app = App(max_age=3600, idle_timeout=600)
        client = app.client()
        client.post("/set?key=user&value=alice")
        app.backend.failing.add("set")
        clock.now += 300
        assert client.get("/session").json() == {"user": "alice"}

    def test_the_backend_drops_sessions_too(self, clock: Clock) -> None:
        stored: list[int | None] = []

        class Recording(Spy):
            async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
                stored.append(expire)
                await super().set(key, value, expire)

        client = App(Recording(), max_age=1000, idle_timeout=600).client()
        client.post("/set?key=user&value=alice")
        clock.now += 500
        client.post("/set?key=user&value=bob")
        clock.now += 499
        client.post("/set?key=user&value=carol")
        assert stored == [600, 500, 1]  # the idle timeout, then what's left of max_age


class TestFailures:
    def test_a_backend_that_cant_be_read(self, caplog: pytest.LogCaptureFixture) -> None:
        app = App()
        client = app.client()
        session_id = issued(client.post("/set?key=user&value=alice"))
        app.backend.failing.add("get")
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.sessions"):
            response = client.get("/session")
        assert (response.status_code, response.json()) == (200, {})
        assert "set-cookie" not in response.headers
        assert "Couldn't read the session" in caplog.text
        assert session_id not in caplog.text

    def test_no_changes_to_a_session_that_couldnt_be_read(self) -> None:
        app = App()
        client = app.client()
        client.post("/set?key=user&value=alice")
        app.backend.failing.add("get")
        for path in ("/set?key=user&value=mallory", "/rotate", "/logout"):
            refused = client.post(path)
            assert (refused.status_code, refused.json()) == (503, {"detail": "Service Unavailable"})
            assert "set-cookie" not in refused.headers
        app.backend.failing.clear()
        assert client.get("/session").json() == {"user": "alice"}

    def test_a_backend_that_cant_be_written(self, caplog: pytest.LogCaptureFixture) -> None:
        app = App()
        app.backend.failing.add("set")
        client = app.client()
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.sessions"):
            refused = client.post("/stream")
        assert (refused.status_code, refused.json()) == (503, {"detail": "Service Unavailable"})
        assert "set-cookie" not in refused.headers
        assert "Couldn't save the session" in caplog.text

    def test_a_logout_that_fails_says_so(self) -> None:
        app = App()
        client = app.client()
        client.post("/set?key=user&value=alice")
        app.backend.failing.add("delete")
        assert client.post("/logout").status_code == 503
        app.backend.failing.clear()
        assert client.get("/session").json() == {"user": "alice"}  # still there: try again

    def test_too_large(self) -> None:
        client = App(max_size=100).client(raise_server_exceptions=False)
        assert client.post("/big?size=80").status_code == 200
        assert client.post("/big?size=100").status_code == 500

    def test_too_large_raises(self) -> None:
        client = App(max_size=100).client()
        with pytest.raises(ValueError, match="more than max_size"):
            client.post("/big?size=100")

    def test_only_json(self) -> None:
        with pytest.raises(TypeError):
            App().client().post("/not-json")


class TestIntegrity:
    def test_unsigned_sessions_are_warned_about_once(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = App().client()
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.sessions"):
            client.get("/public")
            client.get("/public")
        assert caplog.text.count("Sessions are stored unsigned") == 1

    def test_signed_sessions_arent_warned_about(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.sessions"):
            App(cache_options={"signing_key": SIGNING_KEY}).client().get("/public")
        assert "unsigned" not in caplog.text

    def test_planted_sessions_are_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        app = App(cache_options={"signing_key": SIGNING_KEY})
        client = app.client()
        issued(client.post("/set?key=user&value=alice"))
        ((key, _),) = app.backend.sessions().items()
        forged = {"created": 1, "seen": 1, "data": {"user": "admin"}}
        app.backend._state[key].data = json.dumps(forged).encode()
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.sessions"):
            assert client.get("/session").json() == {}
        assert "isn't ours" in caplog.text

    @pytest.mark.parametrize(
        "garbage",
        [
            b"not json",
            b"[]",
            b'"text"',
            b'{"created": 1, "seen": 1}',
            b'{"created": 1, "seen": 1, "data": []}',
            b'{"created": "1", "seen": 1, "data": {}}',
            b'{"created": 1, "seen": true, "data": {}}',
        ],
    )
    def test_garbage_is_ignored(self, garbage: bytes) -> None:
        app = App()
        client = app.client()
        client.post("/set?key=user&value=alice")
        ((key, _),) = app.backend.sessions().items()
        app.backend._state[key].data = garbage
        assert client.get("/session").json() == {}


class TestWebSockets:
    def test_the_session_is_read_only(self) -> None:
        app = App()
        client = app.client()
        session_id = issued(client.post("/set?key=user&value=alice"))
        headers = {"Cookie": f"session={session_id}"}
        with client.websocket_connect("/ws", headers=headers) as ws:
            assert ws.receive_json() == {"user": "alice"}
        assert client.get("/session").json() == {"user": "alice"}

    def test_it_cant_be_rotated(self) -> None:
        with App().client().websocket_connect("/ws/rotate") as ws:
            assert "only be changed in HTTP requests" in ws.receive_text()


class TestHelpers:
    def test_without_the_middleware(self) -> None:
        connection = HTTPConnection({"type": "http", "headers": []})
        for helper in (rotate_session, clear_session):
            with pytest.raises(RuntimeError, match="isn't installed"):
                helper(connection)

    def test_the_session_is_a_dict(self) -> None:
        session = ServerSession({"a": 1})
        assert session == {"a": 1}
        assert not session.accessed
        session.mark_accessed()
        assert session.accessed


class TestConfiguration:
    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"max_age": 0}, "max_age"),
            ({"max_age": 60, "idle_timeout": 61}, "idle_timeout"),
            ({"idle_timeout": 0}, "idle_timeout"),
            ({"max_size": 1}, "max_size"),
            ({"same_site": "none", "secure": False}, "SameSite=None"),
            ({"cookie_name": "__Host-session", "domain": "example.com"}, "no domain"),
        ],
    )
    def test_refused(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            ServerSessionMiddleware(FastAPI(), **options)

    def test_cookie_attributes(self) -> None:
        app = App(
            cookie_name="__Secure-id",
            same_site="strict",
            path="/app",
            domain="testserver",
            max_age=60,
        )
        response = app.client().post("/set?key=user&value=alice")
        assert re.fullmatch(
            r"__Secure-id=[A-Za-z0-9_-]{43}; Max-Age=60; Path=/app; Domain=testserver; "
            r"HttpOnly; Secure; SameSite=Strict",
            response.headers["set-cookie"],
        )
