# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis
import pytest

from fastapi_alcazaba import BackendUnavailable, Cache, FastAPICache
from fastapi_alcazaba.auth import OneTimeCodes, TokenBlocklist
from fastapi_alcazaba.backends import Backend
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.backends.tiered import TieredBackend

NOW = 1_800_000_000.0
SECRET = "s" * 32
OTHER_SECRET = "o" * 32


class Clock:
    """time.time for the expiry dates, time.monotonic for InMemoryBackend's expiry."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = NOW
        monkeypatch.setattr(time, "time", lambda: self.now)
        monkeypatch.setattr(time, "monotonic", lambda: self.now)


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


@pytest.fixture(name="backend", params=["inmemory", "redis"])
def fixture_backend(request: pytest.FixtureRequest) -> Backend:
    if request.param == "inmemory":
        return InMemoryBackend()
    return RedisBackend(fakeredis.FakeAsyncRedis())


@pytest.fixture(name="global_cache")
def fixture_global_cache() -> Iterator[InMemoryBackend]:
    backend = InMemoryBackend()
    FastAPICache.init(backend)
    yield backend
    FastAPICache.reset()


async def stored(backend: Backend) -> list[tuple[str, bytes]]:
    """Every key and value in the backend."""
    if isinstance(backend, InMemoryBackend):
        entries = [*backend._store.items(), *backend._state.items()]
        return [(key, value.data) for key, value in entries]
    client = backend._client  # type: ignore[attr-defined]
    return [(key.decode(), await client.get(key)) for key in await client.keys()]


class Down(InMemoryBackend):
    async def get(self, key: str) -> bytes | None:
        raise ConnectionError("down")

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        raise ConnectionError("down")

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        raise ConnectionError("down")


def other_code(code: str) -> str:
    return f"{(int(code) + 1) % 10 ** len(code):0{len(code)}d}"


@pytest.mark.usefixtures("clock")
class TestRevokedTokens:
    async def test_until_the_token_expires(self, backend: Backend, clock: Clock) -> None:
        blocklist = TokenBlocklist(Cache(backend), leeway=10)
        await blocklist.revoke("jti-1", expires_at=NOW + 60)
        assert await blocklist.is_revoked("jti-1")
        assert not await blocklist.is_revoked("jti-2")
        clock.now += 69  # past exp, but the JWT library still accepts it for 10 s
        assert await blocklist.is_revoked("jti-1")
        clock.now += 2
        assert not await blocklist.is_revoked("jti-1")

    async def test_expiry_as_datetime_or_ttl(self, backend: Backend, clock: Clock) -> None:
        blocklist = TokenBlocklist(Cache(backend), leeway=0)
        exp = datetime.fromtimestamp(NOW, UTC) + timedelta(seconds=30)
        await blocklist.revoke("by-date", expires_at=exp)
        await blocklist.revoke("by-ttl", ttl=60)
        clock.now += 31
        assert not await blocklist.is_revoked("by-date")
        assert await blocklist.is_revoked("by-ttl")

    async def test_tokens_that_cant_be_accepted_anymore_arent_stored(
        self,
        backend: Backend,
    ) -> None:
        blocklist = TokenBlocklist(Cache(backend), leeway=60)
        await blocklist.revoke("long-gone", expires_at=NOW - 61)
        assert await stored(backend) == []
        await blocklist.revoke("within-the-leeway", expires_at=NOW - 30)
        assert await blocklist.is_revoked("within-the-leeway")

    async def test_an_expiry_is_required(self) -> None:
        blocklist = TokenBlocklist(Cache(InMemoryBackend()))
        with pytest.raises(ValueError, match="forever"):
            await blocklist.revoke("jti")
        with pytest.raises(ValueError, match="not both"):
            await blocklist.revoke("jti", expires_at=NOW + 60, ttl=60)
        with pytest.raises(ValueError, match="naive"):
            await blocklist.revoke("jti", expires_at=datetime(2030, 1, 1))

    def test_the_leeway_cant_be_negative(self) -> None:
        with pytest.raises(ValueError, match="leeway"):
            TokenBlocklist(leeway=-1)


@pytest.mark.usefixtures("clock")
class TestLogOutEverywhere:
    async def test_tokens_issued_before(self, backend: Backend, clock: Clock) -> None:
        blocklist = TokenBlocklist(Cache(backend), leeway=0)
        clock.now += 0.5
        await blocklist.revoke_subject("alice", ttl=3600)
        assert await blocklist.is_revoked("old", subject="alice", issued_at=NOW - 10)
        # iat has whole seconds: the tokens of this second can't be told apart, and the one
        # issued right after for the current session must stay valid.
        assert not await blocklist.is_revoked("new", subject="alice", issued_at=NOW)
        assert not await blocklist.is_revoked("old", subject="bob", issued_at=NOW - 10)
        iat = datetime.fromtimestamp(NOW - 10, UTC)
        assert await blocklist.is_revoked("old", subject="alice", issued_at=iat)
        clock.now += 3601
        assert not await blocklist.is_revoked("old", subject="alice", issued_at=NOW - 10)

    async def test_revoked_ids_are_still_refused(self, backend: Backend) -> None:
        blocklist = TokenBlocklist(Cache(backend))
        await blocklist.revoke("jti", ttl=60)
        assert await blocklist.is_revoked("jti", subject="alice", issued_at=NOW)

    async def test_arguments(self) -> None:
        blocklist = TokenBlocklist(Cache(InMemoryBackend()))
        with pytest.raises(ValueError, match="together"):
            await blocklist.is_revoked("jti", subject="alice")
        with pytest.raises(ValueError, match="together"):
            await blocklist.is_revoked("jti", issued_at=NOW)
        with pytest.raises(ValueError, match="ttl"):
            await blocklist.revoke_subject("alice", ttl=0)


@pytest.mark.usefixtures("clock")
class TestBlocklistStorage:
    async def test_nothing_in_clear(self, backend: Backend) -> None:
        blocklist = TokenBlocklist(Cache(backend, prefix="app"))
        await blocklist.revoke("token-id-1234", ttl=60)
        await blocklist.revoke_subject("alice@example.com", ttl=60)
        entries = await stored(backend)
        assert len(entries) == 2
        assert all(key.startswith("app!revoked:") for key, _ in entries)
        assert not any("1234" in key or "alice" in key for key, _ in entries)

    async def test_planted_marks_are_ignored_with_a_signing_key(self, backend: Backend) -> None:
        blocklist = TokenBlocklist(Cache(backend, signing_key="k" * 32))
        await blocklist.revoke("jti", ttl=60)
        await blocklist.revoke_subject("alice", ttl=60)
        assert await blocklist.is_revoked("jti")
        assert await blocklist.is_revoked("other", subject="alice", issued_at=NOW - 1)
        for key, _ in await stored(backend):
            await backend.set(key, str(int(NOW) + 10).encode(), 60)
        assert not await blocklist.is_revoked("jti")
        assert not await blocklist.is_revoked("other", subject="alice", issued_at=NOW - 1)

    async def test_a_cutoff_that_isnt_ours(self, backend: Backend) -> None:
        blocklist = TokenBlocklist(Cache(backend))
        await blocklist.revoke_subject("alice", ttl=60)
        ((key, _),) = await stored(backend)
        await backend.set(key, b"garbage", 60)
        assert not await blocklist.is_revoked("jti", subject="alice", issued_at=NOW - 1)

    @pytest.mark.parametrize(("fail_open", "revoked"), [(False, True), (True, False)])
    async def test_a_failing_backend(
        self,
        fail_open: bool,
        revoked: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        blocklist = TokenBlocklist(Cache(Down()), fail_open=fail_open)
        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.auth.tokens"):
            assert await blocklist.is_revoked("jti") is revoked
            assert await blocklist.is_revoked("jti", subject="a", issued_at=NOW) is revoked
        assert len(caplog.records) == 2
        with pytest.raises(BackendUnavailable):
            await blocklist.revoke("jti", ttl=60)

    async def test_the_global_cache_once_initialised(self) -> None:
        blocklist = TokenBlocklist()  # before init, as at import time
        backend = InMemoryBackend()
        FastAPICache.init(backend)
        try:
            await blocklist.revoke("jti", ttl=60)
            assert await blocklist.is_revoked("jti")
            assert len(backend._state) == 1
        finally:
            FastAPICache.reset()


@pytest.mark.usefixtures("clock")
class TestOneTimeCodes:
    async def test_a_code_works_once(self, backend: Backend) -> None:
        codes = OneTimeCodes(SECRET, Cache(backend))
        code = await codes.issue("alice@example.com", purpose="login")
        assert len(code) == 6
        assert code.isdigit()
        assert not await codes.verify("alice@example.com", other_code(code), purpose="login")
        assert not await codes.verify("alice@example.com", code, purpose="reset-password")
        assert not await codes.verify("bob@example.com", code, purpose="login")
        assert await codes.verify("alice@example.com", code, purpose="login")
        assert not await codes.verify("alice@example.com", code, purpose="login")

    async def test_codes_expire(self, backend: Backend, clock: Clock) -> None:
        codes = OneTimeCodes(SECRET, Cache(backend), ttl=60)
        code = await codes.issue("alice")
        clock.now += 61
        assert not await codes.verify("alice", code)

    async def test_codes_are_lost_after_too_many_guesses(self, backend: Backend) -> None:
        codes = OneTimeCodes(SECRET, Cache(backend), max_attempts=3)
        code = await codes.issue("alice")
        for _ in range(3):
            assert not await codes.verify("alice", other_code(code))
        assert not await codes.verify("alice", code)
        code = await codes.issue("alice")  # a new code, new attempts
        assert await codes.verify("alice", code)

    @pytest.mark.parametrize("malformed", ["12345", "1234567", "12345a", "١٢٣٤٥٦", ""])
    async def test_malformed_codes_count_as_guesses(self, backend: Backend, malformed: str) -> None:
        codes = OneTimeCodes(SECRET, Cache(backend), max_attempts=2)
        code = await codes.issue("alice")
        for _ in range(2):
            assert not await codes.verify("alice", malformed)
        assert not await codes.verify("alice", code)

    async def test_a_new_code_replaces_the_previous_one(self, backend: Backend) -> None:
        codes = OneTimeCodes(SECRET, Cache(backend))
        first = await codes.issue("alice")
        second = await codes.issue("alice")
        if first != second:
            assert not await codes.verify("alice", first)
        assert await codes.verify("alice", second)

    async def test_concurrent_requests_with_the_right_code(self, backend: Backend) -> None:
        codes = OneTimeCodes(SECRET, Cache(backend))
        code = await codes.issue("alice")
        results = await asyncio.gather(*(codes.verify("alice", code) for _ in range(5)))
        assert sorted(results) == [False] * 4 + [True]

    async def test_concurrent_requests_that_all_read_the_code(self) -> None:
        """Every request reads the code before any uses it up: deleting it decides."""

        class Interleaved(InMemoryBackend):
            async def get(self, key: str) -> bytes | None:
                value = await super().get(key)
                await asyncio.sleep(0)  # let the other requests read it too
                return value

        codes = OneTimeCodes(SECRET, Cache(Interleaved()))
        code = await codes.issue("alice")
        results = await asyncio.gather(*(codes.verify("alice", code) for _ in range(5)))
        assert sorted(results) == [False] * 4 + [True]

    async def test_digits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fastapi_alcazaba.auth.tokens.secrets.randbelow", lambda n: 42)
        codes = OneTimeCodes(SECRET, Cache(InMemoryBackend()), digits=8)
        assert await codes.issue("alice") == "00000042"
        assert await codes.verify("alice", "00000042")


@pytest.mark.usefixtures("clock")
class TestCodeStorage:
    async def test_nothing_in_clear(self, backend: Backend) -> None:
        codes = OneTimeCodes(SECRET, Cache(backend, prefix="app"))
        code = await codes.issue("alice@example.com", purpose="login")
        await codes.verify("alice@example.com", other_code(code), purpose="login")
        entries = await stored(backend)
        assert sorted(key.rsplit(":", 1)[1] for key, _ in entries) == ["attempts", "code"]
        assert all(key.startswith("app!otp:") for key, _ in entries)
        assert not any("alice" in key or "login" in key for key, _ in entries)
        assert not any(code.encode() in value for _, value in entries)

    async def test_a_value_copied_from_another_subject_fails(self, backend: Backend) -> None:
        codes = OneTimeCodes(SECRET, Cache(backend))
        mallory = await codes.issue("mallory")
        mallory_mac = dict(await stored(backend))
        await codes.issue("alice")
        alice_key = next(key for key, _ in await stored(backend) if key not in mallory_mac)
        await backend.set(alice_key, next(iter(mallory_mac.values())), 300)
        assert not await codes.verify("alice", mallory)

    async def test_secret_rotation(self, backend: Backend) -> None:
        cache = Cache(backend)
        code = await OneTimeCodes(SECRET, cache).issue("alice")
        assert not await OneTimeCodes(OTHER_SECRET, cache).verify("alice", code)
        assert await OneTimeCodes([OTHER_SECRET, SECRET], cache).verify("alice", code)

    async def test_a_failing_backend(self, caplog: pytest.LogCaptureFixture) -> None:
        codes = OneTimeCodes(SECRET, Cache(Down()))
        with pytest.raises(BackendUnavailable):
            await codes.issue("alice")
        with caplog.at_level(logging.WARNING, logger="fastapi_alcazaba.auth.tokens"):
            assert not await codes.verify("alice", "123456")
        assert len(caplog.records) == 1

    async def test_a_used_code_whose_attempts_cant_be_reset(self) -> None:
        class NoReset(InMemoryBackend):
            failing = False

            async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
                if self.failing and key is not None and key.endswith(":attempts"):
                    raise ConnectionError("down")
                return await super().clear(namespace, key)

        backend = NoReset()
        codes = OneTimeCodes(SECRET, Cache(backend))
        code = await codes.issue("alice")
        backend.failing = True
        assert await codes.verify("alice", code)  # used up all the same; the attempts expire

    async def test_the_global_cache_once_initialised(self, global_cache: InMemoryBackend) -> None:
        codes = OneTimeCodes(SECRET)
        code = await codes.issue("alice")
        assert global_cache._state
        assert await codes.verify("alice", code)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"digits": 3}, "digits"),
            ({"digits": 11}, "digits"),
            ({"ttl": 0}, "ttl"),
            ({"max_attempts": 0}, "max_attempts"),
            ({"secret": "short"}, "32 bytes"),
        ],
    )
    def test_validation(self, kwargs: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            OneTimeCodes(**{"secret": SECRET, **kwargs})


class TestSeveralProcesses:
    """Behind a TieredBackend without pub/sub, each process could answer from its own copy."""

    async def test_a_new_code_replaces_the_old_one_everywhere(self) -> None:
        shared = InMemoryBackend()
        first = OneTimeCodes(SECRET, Cache(TieredBackend(shared, local_ttl=60)))
        second = OneTimeCodes(SECRET, Cache(TieredBackend(shared, local_ttl=60)))
        old = await second.issue("alice")
        assert not await first.verify("alice", other_code(old))  # the first reads the old code
        while (new := await second.issue("alice")) == old:
            pass
        assert not await first.verify("alice", old)
        assert await second.verify("alice", new)

    async def test_a_later_log_out_everywhere_is_seen_everywhere(self, clock: Clock) -> None:
        shared = InMemoryBackend()
        first = TokenBlocklist(Cache(TieredBackend(shared, local_ttl=60)), leeway=0)
        second = TokenBlocklist(Cache(TieredBackend(shared, local_ttl=60)), leeway=0)
        await second.revoke_subject("alice", ttl=3600)
        assert not await first.is_revoked("t", subject="alice", issued_at=NOW + 5)
        clock.now += 10
        await second.revoke_subject("alice", ttl=3600)
        assert await first.is_revoked("t", subject="alice", issued_at=NOW + 5)
