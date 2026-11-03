"""JWTStrategy's keys: JWTKey of any type, rotation by kid, retirement, and the JWKS fetched with
a deadline, a refetch rate and stale keys."""
# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from starlette.websockets import WebSocketDisconnect

from fastapi_alcazaba import FastAPIAuth
from fastapi_alcazaba.auth import AuthenticationFailed
from fastapi_alcazaba.auth.strategies import jwt as jwt_module
from fastapi_alcazaba.auth.strategies.jwt import JWKSUnavailable, JWTKey, JWTStrategy
from fastapi_alcazaba.outbound import ResilientTransport

SECRET = "a-secret-of-at-least-thirty-two-bytes"  # noqa: S105  # the tests' own
OTHER_SECRET = "another-secret-of-at-least-32-bytes!"  # noqa: S105
JWKS_URL = "https://idp.example.com/.well-known/jwks.json"
ISSUER = "https://idp.example.com/"

RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
P256 = ec.generate_private_key(ec.SECP256R1())
P384 = ec.generate_private_key(ec.SECP384R1())
ED25519 = ed25519.Ed25519PrivateKey.generate()


def pem(key: Any) -> bytes:
    if hasattr(key, "private_bytes"):
        return bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return bytes(
        key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def header(token: str) -> dict[str, Any]:
    return jwt.get_unverified_header(token)


def claims(**extra: Any) -> dict[str, Any]:
    return {"sub": "ana", "exp": int(time.time()) + 60, **extra}


def encoded(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def hs256(header_data: dict[str, Any], key: bytes) -> str:
    """A token signed with HMAC-SHA256 by hand, whatever its header and key."""
    signing_input = f"{encoded(header_data)}.{encoded(claims())}"
    signature = hmac.new(key, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


class Clock:
    """Time as the strategy sees it (retirement, the JWKS' age); PyJWT keeps the real one."""

    def __init__(self) -> None:
        self.now = time.time()
        self.ticks = 1_000.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.ticks += seconds


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(jwt_module, "time", clock)
    return clock


@pytest.fixture(name="counted")
def fixture_counted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    outcomes: list[str] = []
    monkeypatch.setattr(jwt_module, "_count", lambda strategy, outcome: outcomes.append(outcome))
    return outcomes


class TestJWTKey:
    @pytest.mark.parametrize(
        ("key", "algorithm"),
        [
            (SECRET, "HS256"),
            ("s" * 48, "HS384"),
            ("s" * 64, "HS512"),
            (RSA, "RS256"),
            (pem(RSA).decode(), "RS512"),
            (RSA, "PS256"),
            (P256, "ES256"),
            (pem(P384), "ES384"),
            (ED25519, "EdDSA"),
        ],
    )
    async def test_every_type_signs_and_verifies(self, key: Any, algorithm: str) -> None:
        signing = JWTKey(key, algorithm=algorithm)
        strategy = JWTStrategy(keys=[signing])
        token = strategy.issue("ana")
        assert header(token) == {"alg": algorithm, "kid": signing.kid, "typ": "JWT"}
        assert (await strategy.decode(token))["sub"] == "ana"
        assert strategy.can_issue
        assert strategy.algorithms == [algorithm]

    async def test_a_public_key_only_verifies(self) -> None:
        signer = JWTStrategy(keys=[JWTKey(P256, algorithm="ES256")])
        verifier = JWTStrategy(keys=[JWTKey(pem(P256.public_key()), algorithm="ES256")])
        assert not verifier.can_issue
        assert not verifier.keys[0].can_sign
        assert verifier.keys[0].kid == signer.keys[0].kid  # the thumbprint of the same key
        assert (await verifier.decode(signer.issue("ana")))["sub"] == "ana"
        with pytest.raises(RuntimeError, match="a key that signs"):
            verifier.issue("ana")

    def test_the_rfc_7638_thumbprint(self) -> None:
        """RFC 7638, 3.1: the thumbprint of its example key."""
        n = (
            "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1"
            "L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4"
            "QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbO"
            "pbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csF"
            "Cur-kEgU8awapJzKnqDKgw"
        )
        public = jwt.algorithms.RSAAlgorithm.from_jwk({"kty": "RSA", "n": n, "e": "AQAB"})
        assert (
            JWTKey(public, algorithm="RS256").kid == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"
        )

    def test_a_secrets_kid_reveals_nothing_of_it(self) -> None:
        kid = JWTKey(SECRET, algorithm="HS256").kid
        digest = base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest()).decode()
        assert len(kid) == 22
        assert not digest.startswith(kid)
        assert kid != JWTKey(OTHER_SECRET, algorithm="HS256").kid
        assert kid == JWTKey(SECRET, algorithm="HS512").kid  # the secret's, whatever the hash
        assert SECRET not in repr(JWTKey(SECRET, algorithm="HS256"))

    def test_a_kid_given(self) -> None:
        key = JWTKey(SECRET, algorithm="HS256", kid="2026-11")
        assert (key.kid, key.kid_given) == ("2026-11", True)
        assert header(JWTStrategy(keys=[key]).issue("ana"))["kid"] == "2026-11"

    @pytest.mark.parametrize(
        ("key", "options", "error", "message"),
        [
            (SECRET, {"algorithm": "none"}, ValueError, "'none' is never"),
            (SECRET, {"algorithm": "HS999"}, ValueError, "unknown algorithm"),
            ([SECRET], {"algorithm": "HS256"}, TypeError, "one key per JWTKey"),
            ("short", {"algorithm": "HS256"}, ValueError, "at least 32 bytes"),
            (pem(RSA.public_key()), {"algorithm": "HS256"}, ValueError, "isn't an HMAC secret"),
            ("not a key", {"algorithm": "RS256"}, ValueError, "not a key for RS256"),
            (P256, {"algorithm": "RS256"}, ValueError, "not a key for RS256"),
            (SECRET, {"algorithm": "HS256", "kid": ""}, ValueError, "non-empty"),
            (
                SECRET,
                {"algorithm": "HS256", "until": datetime(2027, 1, 1)},  # noqa: DTZ001
                ValueError,
                "aware",
            ),
        ],
    )
    def test_refused(
        self,
        key: Any,
        options: dict[str, Any],
        error: type[Exception],
        message: str,
    ) -> None:
        with pytest.raises(error, match=message):
            JWTKey(key, **options)

    def test_encode_and_decode(self) -> None:
        key = JWTKey(SECRET, algorithm="HS256")
        assert key.decode(key.encode(claims()), "HS256")["sub"] == "ana"
        with pytest.raises(jwt.InvalidAlgorithmError, match="isn't for HS512"):
            key.decode(key.encode(claims()), "HS512")

    def test_public_jwk(self) -> None:
        public = JWTKey(RSA, algorithm="RS256").public_jwk()
        assert public["kty"] == "RSA"
        assert "d" not in public  # never the private part
        with pytest.raises(ValueError, match="no public key"):
            JWTKey(SECRET, algorithm="HS256").public_jwk()


class TestRotation:
    async def test_the_old_key_verifies_until_it_retires(self, clock: Clock) -> None:
        old = JWTStrategy(keys=[JWTKey(RSA, algorithm="RS256")])
        token = old.issue("ana", expires_in=3600)
        retires = datetime.fromtimestamp(clock.now, UTC) + timedelta(minutes=30)
        new = JWTStrategy(
            keys=[JWTKey(P256, algorithm="ES256"), JWTKey(RSA, algorithm="RS256", until=retires)]
        )
        assert (await new.decode(token))["sub"] == "ana"
        assert header(new.issue("ana"))["kid"] == new.keys[0].kid  # the new key signs
        clock.advance(30 * 60 + 1)
        with pytest.raises(AuthenticationFailed, match="retired"):
            await new.decode(token)

    async def test_from_hmac_to_rsa(self) -> None:
        """HMAC and asymmetric keys together, each with its algorithm only."""
        hmac_key, rsa_key = JWTKey(SECRET, algorithm="HS256"), JWTKey(RSA, algorithm="RS256")
        before = JWTStrategy(keys=[hmac_key])
        after = JWTStrategy(keys=[rsa_key, hmac_key])
        assert (await after.decode(before.issue("ana")))["sub"] == "ana"
        assert (await after.decode(after.issue("ben")))["sub"] == "ben"
        assert after.algorithms == ["RS256", "HS256"]
        # The RSA public key as an HMAC secret, under the RSA key's kid: never.
        forged = hs256({"alg": "HS256", "kid": rsa_key.kid}, pem(RSA.public_key()))
        with pytest.raises(AuthenticationFailed, match="no key for kid"):
            await after.decode(forged)
        # A token of the HMAC key claiming RS256: refused too.
        wrong = hs256({"alg": "RS256", "kid": hmac_key.kid}, SECRET.encode())
        with pytest.raises(AuthenticationFailed, match="no key for kid"):
            await after.decode(wrong)

    async def test_tokens_without_a_kid(self) -> None:
        """Tokens issued before 0.8 have none: the keys whose kid wasn't given verify them."""
        old_token = jwt.encode(claims(), SECRET, algorithm="HS256")
        assert (await JWTStrategy(SECRET).decode(old_token))["sub"] == "ana"
        derived = JWTStrategy(
            keys=[JWTKey(OTHER_SECRET, algorithm="HS256"), JWTKey(SECRET, algorithm="HS256")]
        )
        assert (await derived.decode(old_token))["sub"] == "ana"
        named = JWTStrategy(keys=[JWTKey(SECRET, algorithm="HS256", kid="k1")])
        with pytest.raises(AuthenticationFailed, match="no key for kid None"):
            await named.decode(old_token)

    async def test_a_kid_no_key_has(self) -> None:
        """An identity provider's token, with its own kid, verified with its public key."""
        token = jwt.encode(claims(), RSA, algorithm="RS256", headers={"kid": "provider-key-7"})
        legacy = JWTStrategy(pem(RSA.public_key()), algorithms=["RS256"])
        assert (await legacy.decode(token))["sub"] == "ana"
        named = JWTStrategy(keys=[JWTKey(RSA.public_key(), algorithm="RS256", kid="mine")])
        with pytest.raises(AuthenticationFailed, match="no key for kid 'provider-key-7'"):
            await named.decode(token)

    async def test_secrets_of_key_rotate_with_kids(self) -> None:
        old = JWTStrategy(OTHER_SECRET)
        new = JWTStrategy([SECRET, OTHER_SECRET])
        assert header(old.issue("ana"))["kid"] == JWTKey(OTHER_SECRET, algorithm="HS256").kid
        assert (await new.decode(old.issue("ana")))["sub"] == "ana"
        with pytest.raises(AuthenticationFailed):
            await old.decode(new.issue("ana"))

    async def test_a_token_signed_by_another_key_under_a_known_kid(self) -> None:
        strategy = JWTStrategy(keys=[JWTKey(RSA, algorithm="RS256")])
        forged = jwt.encode(
            claims(), OTHER_RSA, algorithm="RS256", headers={"kid": strategy.keys[0].kid}
        )
        with pytest.raises(AuthenticationFailed, match="InvalidSignatureError"):
            await strategy.decode(forged)

    @pytest.mark.parametrize(
        "token",
        [
            "not.a.token",
            encoded({"alg": "HS256", "kid": 7}) + ".e30.eA",
            encoded({"alg": 5}) + ".e30.eA",
            encoded({"typ": "JWT"}) + ".e30.eA",
            "e30.e30.x",
        ],
    )
    async def test_malformed(self, token: str) -> None:
        strategy = JWTStrategy(keys=[JWTKey(SECRET, algorithm="HS256")])
        with pytest.raises(AuthenticationFailed, match="invalid token"):
            await strategy.decode(token)

    async def test_alg_none(self) -> None:
        key = JWTKey(SECRET, algorithm="HS256")
        unsigned = f"{encoded({'alg': 'none', 'kid': key.kid})}.{encoded(claims())}."
        with pytest.raises(AuthenticationFailed, match="invalid token"):
            await JWTStrategy(keys=[key]).decode(unsigned)

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"keys": []}, "list of JWTKey"),
            ({"keys": [SECRET]}, "list of JWTKey"),
            (
                {
                    "keys": [
                        JWTKey(SECRET, algorithm="HS256", until=datetime(2030, 1, 1, tzinfo=UTC))
                    ]
                },
                "doesn't retire",
            ),
            (
                {
                    "keys": [
                        JWTKey(SECRET, algorithm="HS256", kid="a"),
                        JWTKey(OTHER_SECRET, algorithm="HS256", kid="a"),
                    ]
                },
                "two keys have one kid",
            ),
            (
                {"keys": [JWTKey(SECRET, algorithm="HS256")], "algorithms": ["HS256"]},
                "no algorithms",
            ),
            (
                {"keys": [JWTKey(SECRET, algorithm="HS256")], "signing_key": SECRET},
                "no signing_key",
            ),
            ({"keys": [JWTKey(SECRET, algorithm="HS256")], "key": SECRET}, "one of them"),
            ({"key": SECRET, "algorithms": ["HS999"]}, "unknown algorithms"),
        ],
    )
    def test_configuration_errors(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            JWTStrategy(**options)


class TestPublishedKeys:
    async def test_jwks(self, clock: Clock) -> None:
        retired = datetime.fromtimestamp(clock.now, UTC) - timedelta(seconds=1)
        strategy = JWTStrategy(
            keys=[
                JWTKey(P256, algorithm="ES256"),
                JWTKey(RSA.public_key(), algorithm="RS256"),
                JWTKey(SECRET, algorithm="HS256"),
                JWTKey(OTHER_RSA, algorithm="RS256", until=retired),
            ]
        )
        keys = strategy.jwks()["keys"]
        assert [(key["kid"], key["alg"], key["use"]) for key in keys] == [
            (strategy.keys[0].kid, "ES256", "sig"),
            (strategy.keys[1].kid, "RS256", "sig"),
        ]
        assert all("d" not in key and "k" not in key for key in keys)  # no private part, no secret
        # Another service verifies the app's tokens with them.
        verifier = remote(Provider(strategy.jwks()))
        token = strategy.issue("ana", aud="api", iss=ISSUER)
        assert (await verifier.decode(token))["sub"] == "ana"


class Provider:
    """Stands in for an identity provider's JWKS endpoint."""

    def __init__(self, jwks: dict[str, Any] | None = None) -> None:
        self.jwks = jwks if jwks is not None else {"keys": [jwk(RSA, "key-1")]}
        self.calls = 0
        self.answer: Callable[[], httpx.Response] | None = None
        self.delay = 0.0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.answer is not None:
            return self.answer()
        return httpx.Response(200, json=self.jwks)


def jwk(key: Any, kid: str | None, alg: str | None = "RS256", **extra: Any) -> dict[str, Any]:
    """The public JWK of `key` (RSA or EC), as a provider publishes it."""
    public = key.public_key() if hasattr(key, "public_key") else key
    if isinstance(public, rsa.RSAPublicKey):
        data = jwt.algorithms.RSAAlgorithm.to_jwk(public, as_dict=True)
    else:
        data = jwt.algorithms.ECAlgorithm.to_jwk(public, as_dict=True)
    if kid is not None:
        data["kid"] = kid
    if alg is not None:
        data["alg"] = alg
    return {**data, **extra}


def remote(provider: Provider, **options: Any) -> JWTStrategy:
    transport = options.pop("transport", None) or httpx.MockTransport(provider.handler)
    return JWTStrategy(
        jwks_url=JWKS_URL,
        audience="api",
        issuer=ISSUER,
        jwks_client=httpx.AsyncClient(transport=transport),
        **options,
    )


def provider_token(key: Any = RSA, kid: str | None = "key-1", algorithm: str = "RS256") -> str:
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(claims(aud="api", iss=ISSUER), key, algorithm=algorithm, headers=headers)


class TestJWKS:
    async def test_fetched_once_and_kept(self, clock: Clock, counted: list[str]) -> None:
        provider = Provider()
        strategy = remote(provider)
        for _ in range(3):
            assert (await strategy.decode(provider_token()))["sub"] == "ana"
        assert provider.calls == 1
        clock.advance(301)  # past jwks_lifespan
        await strategy.decode(provider_token())
        assert provider.calls == 2
        assert counted == ["fetched", "fetched"]

    async def test_unknown_kids_are_fetched_at_most_every_30_seconds(self, clock: Clock) -> None:
        provider = Provider()
        strategy = remote(provider)
        await strategy.decode(provider_token())
        for index in range(20):
            with pytest.raises(AuthenticationFailed, match="isn't the provider's"):
                await strategy.decode(provider_token(kid=f"made-up-{index}"))
        assert provider.calls == 1
        clock.advance(31)
        with pytest.raises(AuthenticationFailed, match="isn't the provider's"):
            await strategy.decode(provider_token(kid="made-up"))
        assert provider.calls == 2

    async def test_a_key_the_provider_rotated_in(self, clock: Clock) -> None:
        provider = Provider()
        strategy = remote(provider)
        await strategy.decode(provider_token())
        provider.jwks = {"keys": [jwk(RSA, "key-1"), jwk(OTHER_RSA, "key-2")]}
        clock.advance(31)
        token = provider_token(OTHER_RSA, kid="key-2")
        assert (await strategy.decode(token))["sub"] == "ana"
        assert provider.calls == 2

    async def test_concurrent_fetches_are_one(self) -> None:
        provider = Provider()
        provider.delay = 0.05
        strategy = remote(provider)
        results = await asyncio.gather(*(strategy.decode(provider_token()) for _ in range(20)))
        assert {result["sub"] for result in results} == {"ana"}
        assert provider.calls == 1

    async def test_stale_keys_while_the_provider_is_down(
        self,
        clock: Clock,
        counted: list[str],
    ) -> None:
        provider = Provider()
        strategy = remote(provider)
        await strategy.decode(provider_token())
        provider.answer = lambda: httpx.Response(503)
        clock.advance(301)
        assert (await strategy.decode(provider_token()))["sub"] == "ana"  # stale, still used
        assert (await strategy.decode(provider_token()))["sub"] == "ana"
        assert provider.calls == 2  # not retried within 30 seconds
        clock.advance(3600)  # past jwks_stale
        with pytest.raises(JWKSUnavailable, match="no keys of"):
            await strategy.decode(provider_token())
        assert counted == ["fetched", "failed", "failed"]
        provider.answer = None  # back
        clock.advance(31)
        assert (await strategy.decode(provider_token()))["sub"] == "ana"

    async def test_a_provider_down_from_the_start(self, counted: list[str]) -> None:
        provider = Provider()
        provider.answer = lambda: httpx.Response(500)
        with pytest.raises(JWKSUnavailable):
            await remote(provider).decode(provider_token())
        assert counted == ["failed"]

    async def test_a_slow_provider(self) -> None:
        provider = Provider()
        provider.delay = 5
        transport = ResilientTransport(httpx.MockTransport(provider.handler), timeout=0.05)
        strategy = remote(provider, transport=transport)
        started = time.perf_counter()
        with pytest.raises(JWKSUnavailable):
            await strategy.decode(provider_token())
        assert time.perf_counter() - started < 1

    async def test_its_own_client_has_a_deadline_and_a_breaker(self) -> None:
        strategy = JWTStrategy(jwks_url=JWKS_URL, audience="api", issuer=ISSUER, jwks_timeout=1.5)
        remote_keys = strategy._remote
        assert remote_keys is not None
        transport = remote_keys.client._transport
        assert isinstance(transport, ResilientTransport)
        assert (transport.timeout, transport.max_response_size) == (1.5, jwt_module.MAX_JWKS)
        assert transport.failure_threshold is not None
        await strategy.aclose()
        assert remote_keys.client.is_closed

    async def test_a_client_given_is_not_closed(self) -> None:
        strategy = remote(Provider())
        await strategy.aclose()
        assert strategy._remote is not None
        assert not strategy._remote.client.is_closed
        await JWTStrategy(SECRET).aclose()  # no JWKS: nothing to close

    @pytest.mark.parametrize(
        "answer",
        [
            lambda: httpx.Response(200, text="<html>maintenance</html>"),
            lambda: httpx.Response(200, json=["not", "a", "jwks"]),
            lambda: httpx.Response(200, json={"keys": "none"}),
            lambda: httpx.Response(
                200, json={"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": "key-1"}]}
            ),
            lambda: httpx.Response(200, json={"keys": [jwk(RSA, "key-1", use="enc")]}),
            lambda: httpx.Response(200, json={"keys": [jwk(RSA, "key-1", alg="RS384")]}),
            lambda: httpx.Response(200, json={"keys": [{"kty": "RSA", "kid": "key-1"}]}),
            lambda: httpx.Response(200, json={"keys": [jwk(RSA, None) | {"kid": 7}]}),
            lambda: httpx.Response(404),
        ],
    )
    async def test_not_a_usable_jwks(
        self,
        answer: Callable[[], httpx.Response],
        counted: list[str],
    ) -> None:
        provider = Provider()
        provider.answer = answer
        with pytest.raises(JWKSUnavailable):
            await remote(provider, algorithms=["RS256"]).decode(provider_token())
        assert counted == ["failed"]

    async def test_the_algorithm_of_each_key(self) -> None:
        provider = Provider(
            {
                "keys": [
                    jwk(RSA, "pinned", alg="RS256"),
                    jwk(RSA, "free", alg=None),
                    jwk(P256, "ec", alg=None),
                ]
            }
        )
        strategy = remote(provider)
        with pytest.raises(AuthenticationFailed, match="isn't for PS256"):
            await strategy.decode(provider_token(kid="pinned", algorithm="PS256"))
        token = provider_token(kid="free", algorithm="PS256")
        assert (await strategy.decode(token))["sub"] == "ana"
        assert (await strategy.decode(token))["sub"] == "ana"  # its parsed key kept
        with pytest.raises(AuthenticationFailed, match="isn't for RS256"):
            await strategy.decode(provider_token(kid="ec"))  # an EC key for RS256
        token = provider_token(P256, kid="ec", algorithm="ES256")
        assert (await strategy.decode(token))["sub"] == "ana"
        with pytest.raises(AuthenticationFailed, match="algorithm isn't accepted"):
            await strategy.decode(provider_token(kid="free", algorithm="RS512"))

    async def test_one_key_without_a_kid(self) -> None:
        provider = Provider({"keys": [jwk(RSA, None)]})
        assert (await remote(provider).decode(provider_token(kid=None)))["sub"] == "ana"
        # A provider with one key may name it, and its tokens not.
        assert (await remote(Provider()).decode(provider_token(kid=None)))["sub"] == "ana"
        two = Provider({"keys": [jwk(RSA, "key-1"), jwk(OTHER_RSA, "key-2")]})
        with pytest.raises(AuthenticationFailed, match="isn't the provider's: None"):
            await remote(two).decode(provider_token(kid=None))

    async def test_an_unavailable_provider_is_a_503(self) -> None:
        provider = Provider()
        provider.answer = lambda: httpx.Response(502)
        strategy = remote(provider)
        auth = FastAPIAuth(strategy)
        app = FastAPI()

        @app.get("/me")
        async def me(user: Annotated[Any, Depends(auth.authenticate())]) -> Any:
            return user

        @app.websocket("/ws")
        async def ws(
            websocket: WebSocket,
            _user: Annotated[Any, Depends(auth.authenticate())],
        ) -> None:
            await websocket.accept()

        client = TestClient(app)
        bearer = {"Authorization": f"Bearer {provider_token()}"}
        response = client.get("/me", headers=bearer)
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/ws", headers=bearer):
                pass
        assert closed.value.code == 1013

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"jwks_url": "http://idp.example.com/jwks"}, "HTTPS"),
            ({"jwks_url": JWKS_URL, "algorithms": ["HS256"]}, "asymmetric"),
            ({"jwks_url": JWKS_URL, "jwks_lifespan": 0}, "positive"),
            ({"jwks_url": JWKS_URL, "jwks_timeout": 0}, "positive"),
            ({"jwks_url": JWKS_URL, "jwks_stale": -1}, "negative"),
        ],
    )
    def test_configuration_errors(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            JWTStrategy(audience="api", issuer=ISSUER, **options)

    async def test_it_doesnt_issue(self) -> None:
        strategy = remote(Provider())
        assert not strategy.can_issue
        assert strategy.keys == []
        with pytest.raises(RuntimeError, match="a key that signs"):
            strategy.issue("ana")

    async def test_localhost_over_http(self) -> None:
        strategy = JWTStrategy(jwks_url="http://localhost:8080/jwks", audience="a", issuer="i")
        assert strategy.algorithms == ["RS256", "ES256", "PS256", "EdDSA"]
        await strategy.aclose()


JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=5), children, max_size=4)
    ),
    max_leaves=12,
)
#: Members of real JWKs, so that some of what's generated gets past the first checks.
JWK_MEMBERS = st.fixed_dictionaries(
    {},
    optional={
        "kty": st.sampled_from(["RSA", "EC", "OKP", "oct", "XYZ"]),
        "kid": st.none() | st.text(max_size=4) | st.integers(),
        "use": st.sampled_from(["sig", "enc", 7]),
        "alg": st.sampled_from(["RS256", "HS256", "ES256", "none", "RS384"]),
        "crv": st.sampled_from(["P-256", "Ed25519", "X9"]),
        "n": st.sampled_from([jwk(RSA, None)["n"], "AQAB", ""]),
        "e": st.just("AQAB"),
        "x": st.sampled_from([jwk(P256, None)["x"], "AA"]),
        "y": st.sampled_from([jwk(P256, None)["y"], "AA"]),
        "k": st.just("c2VjcmV0"),
    },
)


@given(st.one_of(JSON, st.fixed_dictionaries({"keys": st.lists(JWK_MEMBERS | JSON, max_size=4)})))
def test_whatever_a_jwks_holds(data: Any) -> None:
    """Any JSON gives signing keys only, none an HMAC secret nor one PyJWT can't load."""
    for kid, keys in jwt_module._usable_keys(data, jwt_module.JWKS_ALGORITHMS).items():
        assert kid is None or isinstance(kid, str)
        for key in keys:
            assert key.get("kid") == kid
            assert key.get("use", "sig") == "sig"
            assert key.get("kty") != "oct"
            assert key.get("alg") in (None, *jwt_module.JWKS_ALGORITHMS)
            jwt.PyJWK(key)
