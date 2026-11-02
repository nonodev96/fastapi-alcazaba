"""RefreshTokens: rotation, reuse, the grace period, lifetimes, clients and revocation."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import json
import logging
import time
from typing import Any

import pytest
from starlette.requests import Request

from fastapi_alcazaba import BackendUnavailable, Cache, FastAPIAudit, FastAPICache
from fastapi_alcazaba.audit import MemoryAuditSink
from fastapi_alcazaba.auth import (
    AuthenticationFailed,
    RefreshRefused,
    RefreshTokens,
    TokenBlocklist,
    TokenPair,
)
from fastapi_alcazaba.auth import refresh as refresh_module
from fastapi_alcazaba.auth import tokens as tokens_module
from fastapi_alcazaba.auth.refresh import format_token, parse_token
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend

JWT_SECRET = "jwt-secret-of-at-least-32-bytes!!!"  # noqa: S105  # the tests' own
SECRET = "refresh-secret-of-at-least-32-bytes"  # noqa: S105
NEW_SECRET = "a-newer-refresh-secret-of-32-bytes!"  # noqa: S105
DAY = 86_400


class Clock:
    """Time as RefreshTokens sees it; access tokens keep the real clock, which PyJWT checks."""

    def __init__(self) -> None:
        self.now = time.time()

    def time(self) -> float:
        return self.now


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(refresh_module, "time", clock)
    return clock


@pytest.fixture(name="strategy")
def fixture_strategy() -> JWTStrategy:
    return JWTStrategy(JWT_SECRET, blocklist=TokenBlocklist(Cache(InMemoryBackend())))


@pytest.fixture(name="refresh")
def fixture_refresh(strategy: JWTStrategy) -> RefreshTokens:
    return RefreshTokens(strategy, secret=SECRET, cache=Cache(InMemoryBackend()))


async def accepted(strategy: JWTStrategy, token: str) -> bool:
    """Whether the strategy lets the access token in."""
    headers = [(b"authorization", f"Bearer {token}".encode())]
    request = Request({"type": "http", "headers": headers, "path": "/", "method": "GET"})
    try:
        await strategy.authenticate(request)
    except AuthenticationFailed:
        return False
    return True


async def refused(refresh: RefreshTokens, token: str, **options: Any) -> str:
    """Why `token` gets nothing."""
    with pytest.raises(RefreshRefused) as refusal:
        await refresh.exchange(token, **options)
    return refusal.value.reason


class TestIssue:
    async def test_a_pair(self, refresh: RefreshTokens, strategy: JWTStrategy) -> None:
        pair = await refresh.issue("ana", scope="read")
        assert (pair.token_type, pair.expires_in) == ("bearer", 900)
        assert 30 * DAY - 2 <= pair.refresh_expires_in <= 30 * DAY
        claims = await strategy.decode(pair.access_token)
        assert (claims["sub"], claims["sid"], claims["scope"]) == ("ana", pair.session, "read")
        assert claims["exp"] - claims["iat"] == 900
        assert parse_token(pair.refresh_token) == (pair.session, 0, pair.refresh_token[-43:])
        assert await accepted(strategy, pair.access_token)

    async def test_nothing_in_the_backend_makes_a_token(self, refresh: RefreshTokens) -> None:
        cache = refresh.cache
        assert cache is not None
        pair = await refresh.issue("ana")
        backend = cache.backend
        assert isinstance(backend, InMemoryBackend)
        stored = repr([(k, v.data) for place in backend._state.values() for k, v in place.items()])
        assert pair.refresh_token not in stored
        assert pair.refresh_token.rsplit(".", 1)[1] not in stored
        assert pair.session not in stored  # its digest is the key

    @pytest.mark.parametrize("claim", ["sub", "sid", "exp", "iat", "jti", "iss", "aud", "nbf"])
    async def test_reserved_claims(self, refresh: RefreshTokens, claim: str) -> None:
        claims: dict[str, Any] = {claim: "x"}
        with pytest.raises(ValueError, match=f"sets {claim} itself"):
            await refresh.issue("ana", **claims)

    async def test_claims_must_be_json(self, refresh: RefreshTokens) -> None:
        with pytest.raises(TypeError):
            await refresh.issue("ana", when=object())


class TestExchange:
    async def test_rotation(self, refresh: RefreshTokens, strategy: JWTStrategy) -> None:
        first = await refresh.issue("ana", scope="read")
        second = await refresh.exchange(first.refresh_token)
        assert second.session == first.session
        assert parse_token(second.refresh_token)[1] == 1  # type: ignore[index]
        assert second.refresh_token != first.refresh_token
        claims = await strategy.decode(second.access_token)
        assert (claims["sub"], claims["sid"], claims["scope"]) == ("ana", first.session, "read")
        third = await refresh.exchange(second.refresh_token)
        assert parse_token(third.refresh_token)[1] == 2  # type: ignore[index]
        assert await accepted(strategy, third.access_token)

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "not a token",
            "r1.short.0.mac",
            "r2." + "a" * 22 + ".0." + "b" * 43,
            "r1." + "a" * 22 + ".01." + "b" * 43,  # a leading zero
            "r1." + "a" * 22 + ".0." + "b" * 43 + "\n",
            "r1." + "a" * 22 + ".0." + "b" * 300,
            "r1." + "a" * 22 + "." + "9" * 11 + "." + "b" * 43,
        ],
    )
    async def test_malformed(self, refresh: RefreshTokens, token: str) -> None:
        assert await refused(refresh, token) == "malformed"

    async def test_unknown_family(self, refresh: RefreshTokens) -> None:
        assert await refused(refresh, format_token("a" * 22, 0, "b" * 43)) == "unknown"

    async def test_a_forgery_revokes_nothing(
        self,
        refresh: RefreshTokens,
        strategy: JWTStrategy,
    ) -> None:
        """Whoever saw an access token knows its sid: it doesn't let them end the login."""
        pair = await refresh.issue("ana")
        forged = format_token(pair.session, 0, "A" * 43)
        assert await refused(refresh, forged) == "forged"
        assert await refused(refresh, format_token(pair.session, 5, "A" * 43)) == "forged"
        assert not await refresh.revoke(forged)
        assert await accepted(strategy, pair.access_token)
        await refresh.exchange(pair.refresh_token)

    async def test_bound_to_its_subject(self, refresh: RefreshTokens) -> None:
        """A record with another subject (planted, or a bug) makes its tokens forgeries."""
        pair = await refresh.issue("ana")
        cache = refresh.cache
        assert cache is not None
        key = refresh._key(cache, "family", pair.session)
        record = json.loads(await cache.backend.get(key) or b"")
        await cache.backend.set(key, json.dumps(record | {"sub": "eve"}).encode(), 60)
        assert await refused(refresh, pair.refresh_token) == "forged"

    async def test_secrets_rotate(self, strategy: JWTStrategy) -> None:
        cache = Cache(InMemoryBackend())
        old = await RefreshTokens(strategy, secret=SECRET, cache=cache).issue("ana")
        rotated = RefreshTokens(strategy, secret=[NEW_SECRET, SECRET], cache=cache)
        new = await rotated.exchange(old.refresh_token)
        newest_only = RefreshTokens(strategy, secret=NEW_SECRET, cache=cache)
        await newest_only.exchange(new.refresh_token)


class TestReuse:
    async def test_a_token_used_again_ends_the_login(
        self,
        refresh: RefreshTokens,
        strategy: JWTStrategy,
        clock: Clock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        first = await refresh.issue("ana")
        second = await refresh.exchange(first.refresh_token)
        clock.now += 11  # past the grace period
        with caplog.at_level(logging.WARNING, "fastapi_alcazaba.auth.refresh"):
            assert await refused(refresh, first.refresh_token) == "reused"
        assert "used again" in caplog.text
        assert await refused(refresh, second.refresh_token) == "unknown"
        assert not await accepted(strategy, second.access_token)
        assert not await accepted(strategy, first.access_token)

    async def test_any_older_token(self, refresh: RefreshTokens, strategy: JWTStrategy) -> None:
        first = await refresh.issue("ana")
        second = await refresh.exchange(first.refresh_token)
        third = await refresh.exchange(second.refresh_token)
        assert await refused(refresh, first.refresh_token) == "reused"
        assert await refused(refresh, third.refresh_token) == "unknown"
        assert not await accepted(strategy, third.access_token)

    async def test_other_logins_go_on(
        self,
        refresh: RefreshTokens,
        strategy: JWTStrategy,
        clock: Clock,
    ) -> None:
        stolen, other = await refresh.issue("ana"), await refresh.issue("ana")
        await refresh.exchange(stolen.refresh_token)
        clock.now += 11
        assert await refused(refresh, stolen.refresh_token) == "reused"
        assert await accepted(strategy, other.access_token)
        await refresh.exchange(other.refresh_token)


class TestGrace:
    async def test_the_token_just_used_gets_the_newest(
        self,
        refresh: RefreshTokens,
        strategy: JWTStrategy,
        clock: Clock,
    ) -> None:
        first = await refresh.issue("ana")
        second = await refresh.exchange(first.refresh_token)
        clock.now += 10
        again = await refresh.exchange(first.refresh_token)
        assert again.refresh_token == second.refresh_token
        assert again.access_token != second.access_token
        assert await accepted(strategy, again.access_token)
        third = await refresh.exchange(again.refresh_token)
        assert parse_token(third.refresh_token)[1] == 2  # type: ignore[index]

    async def test_concurrent_exchanges(self, refresh: RefreshTokens) -> None:
        """Tabs whose access tokens expired together: all get the same refresh token."""
        pair = await refresh.issue("ana")
        exchanged = await asyncio.gather(*(refresh.exchange(pair.refresh_token) for _ in range(5)))
        assert len({each.refresh_token for each in exchanged}) == 1
        assert len({each.access_token for each in exchanged}) == 5

    async def test_no_grace(self, strategy: JWTStrategy) -> None:
        refresh = RefreshTokens(strategy, secret=SECRET, cache=Cache(InMemoryBackend()), grace=0)
        pair = await refresh.issue("ana")
        await refresh.exchange(pair.refresh_token)
        assert await refused(refresh, pair.refresh_token) == "reused"


class TestLifetimes:
    async def test_the_absolute_lifetime(self, refresh: RefreshTokens, clock: Clock) -> None:
        pair = await refresh.issue("ana")
        for _ in range(5):  # used every 5 days: never idle
            clock.now += 5 * DAY
            pair = await refresh.exchange(pair.refresh_token)
        assert 5 * DAY - 2 <= pair.refresh_expires_in <= 5 * DAY
        clock.now += pair.refresh_expires_in + 1
        assert await refused(refresh, pair.refresh_token) == "expired"
        assert await refused(refresh, pair.refresh_token) == "unknown"  # and deleted

    async def test_the_idle_lifetime(self, refresh: RefreshTokens, clock: Clock) -> None:
        pair = await refresh.issue("ana")
        clock.now += 7 * DAY
        assert await refused(refresh, pair.refresh_token) == "expired"

    async def test_in_the_backend(self, refresh: RefreshTokens, clock: Clock) -> None:
        cache = refresh.cache
        assert cache is not None
        pair = await refresh.issue("ana")
        key = refresh._key(cache, "family", pair.session)
        assert 7 * DAY - 2 <= (await cache.backend.get_with_ttl(key))[0] <= 7 * DAY
        for _ in range(4):
            clock.now += 6 * DAY
            pair = await refresh.exchange(pair.refresh_token)
        clock.now += 4 * DAY
        await refresh.exchange(pair.refresh_token)
        # 2 days to the absolute end: sooner than idle.
        assert 2 * DAY - 2 <= (await cache.backend.get_with_ttl(key))[0] <= 2 * DAY + 2


class TestClients:
    async def test_bound_to_its_client(
        self,
        refresh: RefreshTokens,
        strategy: JWTStrategy,
    ) -> None:
        pair = await refresh.issue("ana", client="ios")
        pair = await refresh.exchange(pair.refresh_token, client="ios")
        assert await refused(refresh, pair.refresh_token, client="android") == "wrong_client"
        assert await refused(refresh, pair.refresh_token, client="ios") == "unknown"
        assert not await accepted(strategy, pair.access_token)

    async def test_without_its_client(self, refresh: RefreshTokens) -> None:
        pair = await refresh.issue("ana", client="ios")
        assert await refused(refresh, pair.refresh_token) == "wrong_client"

    async def test_unbound(self, refresh: RefreshTokens) -> None:
        pair = await refresh.issue("ana")
        await refresh.exchange(pair.refresh_token, client="anything")


class TestRevocation:
    async def test_log_out(self, refresh: RefreshTokens, strategy: JWTStrategy) -> None:
        pair = await refresh.issue("ana")
        assert await refresh.revoke(pair.refresh_token)
        assert await refused(refresh, pair.refresh_token) == "unknown"
        assert not await accepted(strategy, pair.access_token)
        assert not await refresh.revoke(pair.refresh_token)
        assert not await refresh.revoke("not a token")

    async def test_a_session(self, refresh: RefreshTokens, strategy: JWTStrategy) -> None:
        pair, other = await refresh.issue("ana"), await refresh.issue("ana")
        await refresh.revoke_session(pair.session)
        assert await refused(refresh, pair.refresh_token) == "unknown"
        assert not await accepted(strategy, pair.access_token)
        assert await accepted(strategy, other.access_token)
        await refresh.revoke_session("a" * 22)  # unknown: nothing to do

    async def test_everywhere(
        self,
        refresh: RefreshTokens,
        strategy: JWTStrategy,
        clock: Clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before, others = await refresh.issue("ana"), await refresh.issue("bob")
        clock.now += 5
        later = Clock()
        later.now = clock.now
        monkeypatch.setattr(tokens_module, "time", later)  # the blocklist's cutoff, 5 s later
        await refresh.revoke_subject("ana")
        assert await refused(refresh, before.refresh_token) == "revoked"
        assert await refused(refresh, before.refresh_token) == "unknown"
        assert not await accepted(strategy, before.access_token)
        await refresh.exchange(others.refresh_token)
        after = await refresh.issue("ana")  # the session that changed the password
        await refresh.exchange(after.refresh_token)

    async def test_a_cutoff_that_isnt_ours(self, refresh: RefreshTokens) -> None:
        cache = refresh.cache
        assert cache is not None
        pair = await refresh.issue("ana")
        await cache.backend.set(refresh._key(cache, "subject", "ana"), b"not a time", 60)
        await refresh.exchange(pair.refresh_token)


class TestBackend:
    async def test_records_that_cant_be_read(
        self,
        refresh: RefreshTokens,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cache = refresh.cache
        assert cache is not None
        pairs = [await refresh.issue("ana") for _ in range(4)]
        for pair, stored in zip(
            pairs, [b"not json", b"[]", b'{"sub": "ana"}', b'{"sub": 1}'], strict=True
        ):
            await cache.backend.set(refresh._key(cache, "family", pair.session), stored, 60)
            assert await refused(refresh, pair.refresh_token) == "unknown"
        assert "can't be read" in caplog.text

    @pytest.mark.parametrize(
        "change",
        [
            {"salt": "not base64!"},
            {"client": 1},
            {"claims": []},
            {"created": 1.5},
            {"generation": -1},
            {"used": True},
        ],
    )
    async def test_fields_of_the_wrong_kind(
        self,
        refresh: RefreshTokens,
        change: dict[str, Any],
    ) -> None:
        cache = refresh.cache
        assert cache is not None
        pair = await refresh.issue("ana")
        key = refresh._key(cache, "family", pair.session)
        record = json.loads(await cache.backend.get(key) or b"")
        await cache.backend.set(key, json.dumps(record | change).encode(), 60)
        assert await refused(refresh, pair.refresh_token) == "unknown"

    async def test_sealed_with_the_caches_key(self, strategy: JWTStrategy) -> None:
        cache = Cache(InMemoryBackend(), signing_key="k" * 32)
        refresh = RefreshTokens(strategy, secret=SECRET, cache=cache)
        pair = await refresh.issue("ana")
        await refresh.exchange(pair.refresh_token)
        key = refresh._key(cache, "family", pair.session)
        stored = await cache.backend.get(key)
        assert stored is not None
        await cache.backend.set(key, stored.replace(b'"generation":1', b'"generation":0'), 60)
        assert await refused(refresh, pair.refresh_token) == "unknown"  # planted: ignored

    async def test_fails_closed(self, strategy: JWTStrategy) -> None:
        class Down(InMemoryBackend):
            async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
                raise ConnectionError("down")

        refresh = RefreshTokens(strategy, secret=SECRET, cache=Cache(InMemoryBackend()))
        pair = await refresh.issue("ana")
        assert refresh.cache is not None
        refresh.cache.backend.__class__ = Down
        with pytest.raises(BackendUnavailable):
            await refresh.exchange(pair.refresh_token)
        with pytest.raises(BackendUnavailable):
            await refresh.issue("ana")

    async def test_a_lock_held_too_long(
        self,
        refresh: RefreshTokens,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(refresh_module, "_LOCK_WAIT", 0.05)
        cache = refresh.cache
        assert cache is not None
        pair = await refresh.issue("ana")
        held = cache.lock(f"refresh:{pair.session}", ttl=5, renew=False)
        assert await held.acquire()
        with pytest.raises(BackendUnavailable, match="lock"):
            await refresh.exchange(pair.refresh_token)
        await held.release()
        await refresh.exchange(pair.refresh_token)

    async def test_the_default_cache(self, strategy: JWTStrategy) -> None:
        FastAPICache.init(InMemoryBackend())
        refresh = RefreshTokens(strategy, secret=SECRET)
        pair = await refresh.issue("ana")
        await refresh.exchange(pair.refresh_token)


class WithoutLocks(InMemoryBackend):
    supports_locks = False


class TestSetup:
    def test_a_strategy_that_cant_sign(self) -> None:
        public = JWTStrategy(
            "-----BEGIN PUBLIC KEY-----\n...", algorithms=["RS256"], blocklist=TokenBlocklist()
        )
        with pytest.raises(ValueError, match="can't sign"):
            RefreshTokens(public, secret=SECRET)

    def test_a_strategy_without_a_blocklist(self) -> None:
        with pytest.raises(ValueError, match="blocklist"):
            RefreshTokens(JWTStrategy(JWT_SECRET), secret=SECRET)

    async def test_a_backend_without_locks(self, strategy: JWTStrategy) -> None:
        with pytest.raises(NotImplementedError, match="can't hold locks.*shipped"):
            RefreshTokens(strategy, secret=SECRET, cache=Cache(WithoutLocks()))
        FastAPICache.init(WithoutLocks())
        refresh = RefreshTokens(strategy, secret=SECRET)
        with pytest.raises(NotImplementedError, match="can't hold locks"):
            await refresh.issue("ana")

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"access_ttl": 0}, "access_ttl"),
            ({"idle": 0}, "idle"),
            ({"idle": 31 * DAY}, "idle"),
            ({"grace": -1}, "grace"),
            ({"grace": 61}, "grace"),
            ({"secret": "short"}, "32"),
        ],
    )
    def test_options(self, strategy: JWTStrategy, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            RefreshTokens(strategy, **{"secret": SECRET, **options})


async def test_the_audit_trail(strategy: JWTStrategy, clock: Clock) -> None:
    sink = MemoryAuditSink()
    refresh = RefreshTokens(
        strategy, secret=SECRET, cache=Cache(InMemoryBackend()), audit=FastAPIAudit(sinks=[sink])
    )
    first = await refresh.issue("ana")
    second = await refresh.exchange(first.refresh_token)
    await refresh.exchange(first.refresh_token)  # grace
    clock.now += 11
    with pytest.raises(RefreshRefused):
        await refresh.exchange(first.refresh_token)
    with pytest.raises(RefreshRefused):
        await refresh.exchange("garbage")
    other = await refresh.issue("ana")
    await refresh.revoke(other.refresh_token)
    await refresh.revoke_session(other.session)
    await refresh.revoke_subject("ana")
    assert [(e.action, e.outcome, e.actor, e.reason) for e in sink.events] == [
        ("auth.refresh.issue", "success", "ana", None),
        ("auth.refresh.exchange", "success", "ana", None),
        ("auth.refresh.exchange", "success", "ana", None),
        ("auth.refresh.exchange", "failure", "ana", "reused"),
        ("auth.refresh.exchange", "failure", None, "malformed"),
        ("auth.refresh.issue", "success", "ana", None),
        ("auth.refresh.revoke", "success", "ana", None),
        ("auth.refresh.revoke", "success", None, None),
        ("auth.refresh.revoke", "success", "ana", None),
    ]
    events = json.dumps([e.to_dict() for e in sink.events])
    assert first.refresh_token not in events
    assert second.refresh_token not in events


async def test_the_token_pair_is_a_response() -> None:
    fields = {"access_token": "a", "refresh_token": "r", "expires_in": 900}
    pair = TokenPair.model_validate(fields | {"refresh_expires_in": 60, "session": "s"})
    assert pair.model_dump()["token_type"] == "bearer"  # noqa: S105  # the type, not a token


@pytest.mark.parametrize("ttl", [0, -1])
async def test_a_blocklist_session_needs_a_ttl(ttl: int) -> None:
    with pytest.raises(ValueError, match="ttl"):
        await TokenBlocklist(Cache(InMemoryBackend())).revoke_session("s", ttl=ttl)


async def test_the_blocklist_checks_sessions() -> None:
    blocklist = TokenBlocklist(Cache(InMemoryBackend()))
    await blocklist.revoke_session("s1", ttl=60)
    assert await blocklist.is_revoked("jti", session="s1")
    assert not await blocklist.is_revoked("jti", session="s2")
    assert await blocklist.is_revoked("jti", subject="u", issued_at=0, session="s1")
    assert not await blocklist.is_revoked("jti")
