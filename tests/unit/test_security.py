# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import warnings
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, SecretBytes, SecretStr
from starlette.testclient import TestClient

from fastapi_alcazaba import FastAPICache, UnsignedEntriesWarning
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.coder import PickleCoder
from fastapi_alcazaba.decorator import cache
from fastapi_alcazaba.integrity import Signer
from tests.cache_status import cache_status, served

KEY = "k" * 32
OTHER_KEY = "o" * 32


def current_user(request: Request) -> str:
    return request.headers.get("authorization", "anonymous")


def make_app(**endpoint_options: Any) -> tuple[FastAPI, list[str]]:
    """An endpoint whose result depends on the user, who only reaches it through a router
    dependency: it isn't one of the endpoint's arguments, so it isn't in the default key."""
    calls: list[str] = []
    router = APIRouter(dependencies=[Depends(current_user)])

    @router.get("/me")
    @cache(expire=60, **endpoint_options)
    async def me(request: Request) -> dict[str, str]:
        user = current_user(request)
        calls.append(user)
        return {"user": user}

    app = FastAPI()
    app.include_router(router)
    return app, calls


class TestPerUserEntries:
    def test_users_never_get_each_others_response(self) -> None:
        FastAPICache.init(InMemoryBackend())
        app, calls = make_app()
        with TestClient(app) as client:
            alice = client.get("/me", headers={"Authorization": "Bearer alice"})
            bob = client.get("/me", headers={"Authorization": "Bearer bob"})
            again = client.get("/me", headers={"Authorization": "Bearer alice"})
        assert alice.json() == {"user": "Bearer alice"}
        assert bob.json() == {"user": "Bearer bob"}
        assert served(bob.headers) == "miss"
        assert served(again.headers) == "hit"
        assert calls == ["Bearer alice", "Bearer bob"]

    def test_cookies_get_their_own_entries(self) -> None:
        FastAPICache.init(InMemoryBackend())
        app, calls = make_app()
        with TestClient(app) as client:
            client.get("/me", headers={"Cookie": "session=1"})
            response = client.get("/me", headers={"Cookie": "session=2"})
        assert served(response.headers) == "miss"
        assert len(calls) == 2

    def test_per_user_responses_are_private(self) -> None:
        FastAPICache.init(InMemoryBackend())
        app, _ = make_app()
        with TestClient(app) as client:
            response = client.get("/me", headers={"Authorization": "Bearer alice"})
        assert response.headers["Cache-Control"] == "private, max-age=60"
        assert response.headers["Vary"] == "Authorization, Cookie"

    def test_anonymous_responses_are_shared_but_say_what_they_vary_on(self) -> None:
        FastAPICache.init(InMemoryBackend())
        app, calls = make_app()
        with TestClient(app) as client:
            client.get("/me")
            response = client.get("/me")
        assert served(response.headers) == "hit"
        assert response.headers["Cache-Control"] == "max-age=60"
        assert response.headers["Vary"] == "Authorization, Cookie"
        assert calls == ["anonymous"]

    def test_public_endpoints_can_opt_out(self) -> None:
        FastAPICache.init(InMemoryBackend())
        app, calls = make_app(vary_headers=())
        with TestClient(app) as client:
            client.get("/me", headers={"Authorization": "Bearer alice"})
            response = client.get("/me", headers={"Authorization": "Bearer bob"})
        assert response.json() == {"user": "Bearer alice"}  # what opting out means
        assert "Vary" not in response.headers
        assert response.headers["Cache-Control"] == "max-age=60"
        assert len(calls) == 1

    def test_custom_vary_headers(self) -> None:
        FastAPICache.init(InMemoryBackend(), vary_headers=["X-Api-Key"])
        app, calls = make_app()
        with TestClient(app) as client:
            client.get("/me", headers={"X-Api-Key": "1"})
            response = client.get("/me", headers={"X-Api-Key": "2"})
        assert response.headers["Vary"] == "X-Api-Key"
        assert len(calls) == 2


class TestSignedEntries:
    def _setup(self, **init: Any) -> tuple[FastAPI, list[str], InMemoryBackend]:
        backend = InMemoryBackend()
        FastAPICache.init(backend, signing_key=KEY, **init)
        app, calls = make_app()
        return app, calls, backend

    def test_signed_entries_are_served(self) -> None:
        app, calls, backend = self._setup()
        with TestClient(app) as client:
            client.get("/me")
            response = client.get("/me")
        assert served(response.headers) == "hit"
        assert len(calls) == 1
        stored = next(iter(backend._store.values())).data
        assert stored.startswith(b"fastapi_alcazaba/s1")

    def test_tampered_entries_are_ignored(self) -> None:
        app, calls, backend = self._setup()
        with TestClient(app) as client:
            client.get("/me")
            for value in backend._store.values():
                value.data = value.data.replace(b"anonymous", b"evil-data")
            response = client.get("/me")
        assert response.json() == {"user": "anonymous"}
        assert served(response.headers) == "miss"
        assert len(calls) == 2

    def test_unsigned_entries_planted_in_the_backend_are_ignored(self) -> None:
        app, calls, backend = self._setup()
        with TestClient(app) as client:
            client.get("/me")
            for value in backend._store.values():
                value.data = b'{"user": "planted"}'
            response = client.get("/me")
        assert response.json() == {"user": "anonymous"}
        assert len(calls) == 2

    def test_entries_cant_be_moved_to_another_key(self) -> None:
        """Copying Alice's entry under Bob's key doesn't give Bob Alice's data."""
        app, calls, backend = self._setup()
        with TestClient(app) as client:
            client.get("/me", headers={"Authorization": "alice"})
            client.get("/me", headers={"Authorization": "bob"})
            (_, alice_value), (bob_key, _) = backend._store.items()
            backend._store[bob_key].data = alice_value.data
            response = client.get("/me", headers={"Authorization": "bob"})
        assert response.json() == {"user": "bob"}
        assert calls == ["alice", "bob", "bob"]

    def test_keys_can_be_rotated(self) -> None:
        backend = InMemoryBackend()
        FastAPICache.init(backend, signing_key=OTHER_KEY)
        app, calls = make_app()
        with TestClient(app) as client:
            client.get("/me")
            FastAPICache.reset()
            FastAPICache.init(backend, signing_key=[KEY, OTHER_KEY])
            response = client.get("/me")
        assert served(response.headers) == "hit"
        assert len(calls) == 1

    def test_short_keys_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 32 bytes"):
            FastAPICache.init(InMemoryBackend(), signing_key="short")
        with pytest.raises(ValueError, match="at least 32 bytes"):
            Signer(SecretStr("short"))

    def test_keys_from_pydantic_settings(self) -> None:
        signed = Signer(KEY).sign("key", b"payload")
        assert Signer(SecretStr(KEY)).sign("key", b"payload") == signed
        rotated = Signer([SecretBytes(OTHER_KEY.encode()), SecretStr(KEY)])
        assert rotated.unsign("key", signed) == b"payload"
        FastAPICache.init(InMemoryBackend(), signing_key=SecretStr(KEY))
        assert FastAPICache.get_options().protector is not None

    def test_signer(self) -> None:
        signer = Signer(KEY)
        signed = signer.sign("key", b"payload")
        assert signer.unsign("key", signed) == b"payload"
        assert signer.unsign("other-key", signed) is None
        assert signer.unsign("key", signed[:-1] + b"X") is None
        assert signer.unsign("key", b"payload") is None
        assert Signer(OTHER_KEY).unsign("key", signed) is None


class TestPickleCoderNeedsSigning:
    def test_warns_at_init(self) -> None:
        with pytest.warns(UnsignedEntriesWarning, match="PickleCoder"):
            FastAPICache.init(InMemoryBackend(), coder=PickleCoder)

    def test_no_warning_with_a_signing_key(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            FastAPICache.init(InMemoryBackend(), coder=PickleCoder, signing_key=KEY)

    async def test_warns_when_an_endpoint_uses_it(self) -> None:
        FastAPICache.init(InMemoryBackend())

        @cache(coder=PickleCoder)
        async def pickled() -> int:
            return 1

        with pytest.warns(UnsignedEntriesWarning):
            assert await pickled() == 1


class TestOnlySuccessfulResponsesAreCached:
    def _client(self) -> tuple[TestClient, list[str]]:
        FastAPICache.init(InMemoryBackend())
        calls: list[str] = []
        app = FastAPI()

        @app.get("/returned-error")
        @cache(expire=60)
        async def returned_error() -> JSONResponse:
            calls.append("returned-error")
            return JSONResponse({"error": "not found"}, status_code=404)

        @app.get("/status-on-response")
        @cache(expire=60)
        async def status_on_response(response: Response) -> dict[str, str]:
            calls.append("status-on-response")
            response.status_code = 503
            return {"error": "try later"}

        @app.get("/no-store")
        @cache(expire=60)
        async def no_store(response: Response) -> dict[str, str]:
            calls.append("no-store")
            response.headers["Cache-Control"] = "no-store"
            return {"secret": "value"}

        return TestClient(app), calls

    @pytest.mark.parametrize("path", ["/returned-error", "/status-on-response", "/no-store"])
    def test_not_cached(self, path: str) -> None:
        client, calls = self._client()
        with client:
            first = client.get(path)
            second = client.get(path)
        assert first.status_code == second.status_code
        assert cache_status(second.headers) == "fwd=miss"  # not stored
        assert "ETag" not in second.headers
        assert len(calls) == 2


class Item(BaseModel):
    name: str


class TestBadEntriesAreRecomputed:
    def _client(self) -> tuple[TestClient, InMemoryBackend, list[int]]:
        backend = InMemoryBackend()
        FastAPICache.init(backend)
        calls: list[int] = []
        app = FastAPI()

        @app.get("/item")
        @cache(expire=60)
        async def item() -> Item:
            calls.append(1)
            return Item(name="thing")

        return TestClient(app), backend, calls

    @pytest.mark.parametrize("stored", [b"not json", b'{"renamed_field": "thing"}'])
    def test_undecodable_entries(self, stored: bytes) -> None:
        """Corrupt, or stored before the model changed: a miss, not a 500."""
        client, backend, calls = self._client()
        with client:
            client.get("/item")
            for value in backend._store.values():
                value.data = stored
            response = client.get("/item")
        assert response.status_code == 200
        assert response.json() == {"name": "thing"}
        assert served(response.headers) == "miss"
        assert len(calls) == 2


class FailingBackend(InMemoryBackend):
    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        raise ConnectionError("backend down")

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        raise ConnectionError("backend down")


async def test_backend_errors_do_not_break_the_endpoint() -> None:
    FastAPICache.init(FailingBackend())

    @cache(expire=60)
    async def value() -> int:
        return 42

    assert await value() == 42


class TestSingleFlight:
    async def test_concurrent_misses_compute_once(self) -> None:
        FastAPICache.init(InMemoryBackend())
        calls: list[int] = []

        @cache(expire=60)
        async def slow(x: int) -> int:
            calls.append(x)
            await asyncio.sleep(0.05)
            return x * 2

        assert await asyncio.gather(*(slow(3) for _ in range(10))) == [6] * 10
        assert calls == [3]

    async def test_waiting_calls_compute_when_the_first_one_fails(self) -> None:
        FastAPICache.init(InMemoryBackend())
        calls: list[int] = []

        @cache(expire=60)
        async def flaky() -> int:
            calls.append(1)
            await asyncio.sleep(0.05)
            if len(calls) == 1:
                raise RuntimeError("first call fails")
            return 7

        results = await asyncio.gather(flaky(), flaky(), return_exceptions=True)
        assert isinstance(results[0], RuntimeError)
        assert results[1] == 7

    async def test_can_be_disabled(self) -> None:
        FastAPICache.init(InMemoryBackend(), single_flight=False)
        calls: list[int] = []

        @cache(expire=60)
        async def slow() -> int:
            calls.append(1)
            await asyncio.sleep(0.05)
            return 1

        await asyncio.gather(slow(), slow(), slow())
        assert len(calls) == 3
