"""OIDCStrategy: an OpenID Connect login whose ID token is verified as OpenID Connect Core says,
against a fake provider that signs real tokens and publishes its keys."""

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapi_alcazaba.auth import AuthenticationFailed, FastAPIAuth
from fastapi_alcazaba.auth.strategies import OIDCStrategy
from fastapi_alcazaba.auth.strategies import oidc as oidc_module

ISSUER = "https://idp.example.com"
CLIENT_ID = "the-app"
ACCESS_TOKEN = "at-123"  # noqa: S105  # the fake provider's
SECRET = "s3cret"  # noqa: S105  # the tests' client secret
PROXY = "https://proxy.example.com/token"


def at_hash(token: str, algorithm: Callable[[bytes], Any] = hashlib.sha256) -> str:
    digest = algorithm(token.encode()).digest()
    return base64.urlsafe_b64encode(digest[: len(digest) // 2]).decode().rstrip("=")


class Provider:
    """Stands in for an OpenID Connect provider: discovery, keys, token and userinfo endpoints.
    Each code it's given gets an ID token with the claims of `claims`, and the login's nonce."""

    def __init__(self) -> None:
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())
        self.keys = [self.jwk(self.rsa, "rsa-1", "RS256"), self.jwk(self.ec, "ec-1", "ES256")]
        self.requests: list[httpx.Request] = []
        self.nonce = ""
        self.claims: dict[str, Any] = {}
        self.header: dict[str, Any] = {"kid": "rsa-1"}
        self.algorithm = "RS256"
        self.signing_key: Any = self.rsa
        self.id_token: str | None = None  # a token of its own instead of a signed one
        self.issuer = ISSUER
        self.userinfo: dict[str, Any] = {"sub": "user-1", "email": "ana@example.com"}

    @staticmethod
    def jwk(key: Any, kid: str, algorithm: str) -> dict[str, Any]:
        """The public key of `key`, as the provider's JWKS has it."""
        algorithms = jwt.algorithms
        kind = algorithms.RSAAlgorithm if algorithm.startswith("RS") else algorithms.ECAlgorithm
        data: dict[str, Any] = json.loads(kind.to_jwk(key.public_key()))
        return {**data, "kid": kid, "use": "sig", "alg": algorithm}

    def sign(self, **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "sub": "user-1",
            "aud": CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "nonce": self.nonce,
            "at_hash": at_hash(ACCESS_TOKEN),
            "email": "ana@example.com",
            **self.claims,
            **claims,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        return jwt.encode(payload, self.signing_key, algorithm=self.algorithm, headers=self.header)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "userinfo_endpoint": f"{ISSUER}/userinfo",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json={"keys": self.keys})
        if path == "/token":
            token = self.id_token if self.id_token is not None else self.sign()
            tokens = {"access_token": ACCESS_TOKEN, "token_type": "Bearer", "id_token": token}
            return httpx.Response(200, json={k: v for k, v in tokens.items() if v != ""})
        if path == "/userinfo":
            assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)

    def fetched(self, path: str) -> int:
        return sum(request.url.path == path for request in self.requests)


def strategy(provider: Provider, **options: Any) -> OIDCStrategy:
    client = httpx.AsyncClient(transport=httpx.MockTransport(provider.handler))
    return OIDCStrategy(
        **{
            "name": "idp",
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "client_secret": SECRET,
            "redirect_uri": "https://testserver/callback",
            "client": client,
            **options,
        }
    )


def app_of(oidc: OIDCStrategy) -> TestClient:
    auth = FastAPIAuth(oidc)
    app = FastAPI()

    @app.get("/login")
    async def login() -> Any:
        return oidc.redirect()

    @app.get("/callback")
    async def callback(user: Annotated[Any, Depends(auth.authenticate("idp"))]) -> Any:
        return user

    return TestClient(app, base_url="https://testserver", follow_redirects=False)


async def logged_in(provider: Provider, **options: Any) -> tuple[TestClient, OIDCStrategy]:
    oidc = strategy(provider, **options)
    await oidc.discover()
    return app_of(oidc), oidc


def log_in(client: TestClient, provider: Provider) -> Any:
    """The login: to the provider and back with a code; the callback's response."""
    location = client.get("/login").headers["location"]
    query = parse_qs(urlsplit(location).query)
    provider.nonce = query["nonce"][0]
    return client.get("/callback", params={"code": "the-code", "state": query["state"][0]})


class TestLogin:
    async def test_the_claims_are_the_user(self) -> None:
        provider = Provider()
        client, _ = await logged_in(provider)
        user = log_in(client, provider).json()
        assert (user["sub"], user["email"], user["iss"]) == ("user-1", "ana@example.com", ISSUER)

    async def test_the_redirect_has_a_nonce_kept_in_the_cookie(self) -> None:
        provider = Provider()
        client, _ = await logged_in(provider, max_age=600, params={"prompt": "login"})
        redirect = client.get("/login")
        location = urlsplit(redirect.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/authorize"
        query = {key: values[0] for key, values in parse_qs(location.query).items()}
        assert query["scope"] == "openid email profile"
        assert (query["max_age"], query["prompt"]) == ("600", "login")
        cookie = redirect.headers["set-cookie"].split(";")[0].partition("=")[2]
        state, _, rest = cookie.partition(".")
        assert state == query["state"]
        assert rest.split(".")[1] == query["nonce"]

    async def test_verify_and_userinfo(self) -> None:
        provider = Provider()
        seen: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

        async def find_user(claims: dict[str, Any], userinfo: dict[str, Any] | None) -> str:
            seen.append((claims, userinfo))
            return f"user:{claims['sub']}"

        client, _ = await logged_in(provider, verify=find_user, userinfo=True)
        assert log_in(client, provider).json() == "user:user-1"
        assert seen[0][1] == {"sub": "user-1", "email": "ana@example.com"}

    async def test_userinfo_of_another_subject_is_refused(self) -> None:
        provider = Provider()
        provider.userinfo = {"sub": "user-2"}
        client, _ = await logged_in(provider, userinfo=True)
        assert log_in(client, provider).status_code == 401

    async def test_verify_may_refuse(self) -> None:
        provider = Provider()
        client, _ = await logged_in(provider, verify=lambda claims, userinfo: None)
        assert log_in(client, provider).status_code == 401

    async def test_an_ec_key(self) -> None:
        provider = Provider()
        provider.header, provider.algorithm = {"kid": "ec-1"}, "ES256"
        provider.signing_key = provider.ec
        client, _ = await logged_in(provider)
        assert log_in(client, provider).status_code == 200

    async def test_explicit_endpoints_need_no_discovery(self) -> None:
        provider = Provider()
        oidc = strategy(
            provider,
            authorization_url=f"{ISSUER}/authorize",
            token_url=f"{ISSUER}/token",
            jwks_url=f"{ISSUER}/jwks",
        )
        assert log_in(app_of(oidc), provider).status_code == 200
        assert provider.fetched("/.well-known/openid-configuration") == 0


def refused(provider: Provider, client: TestClient) -> str:
    response = log_in(client, provider)
    assert response.status_code == 401
    return str(response.json())


class TestTheIDToken:
    """Each check of OpenID Connect Core, 3.1.3.7, refuses the login when it fails."""

    @pytest.mark.parametrize(
        ("claims", "reason"),
        [
            ({"iss": "https://evil.example.com"}, "InvalidIssuerError"),
            ({"aud": "another-app"}, "InvalidAudienceError"),
            ({"aud": [CLIENT_ID, "another-app"]}, "another client (azp)"),  # no azp
            ({"azp": "another-app"}, "another client (azp)"),
            ({"exp": int(time.time()) - 3600}, "ExpiredSignatureError"),
            ({"iat": int(time.time()) + 3600}, "ImmatureSignatureError"),
            ({"iat": None}, "MissingRequiredClaimError"),
            ({"sub": None}, "MissingRequiredClaimError"),
            ({"nonce": "another-login"}, "nonce isn't the login's"),
            ({"nonce": None}, "nonce isn't the login's"),
            ({"at_hash": at_hash("another-access-token")}, "at_hash isn't the access token's"),
        ],
    )
    async def test_refused(
        self,
        claims: dict[str, Any],
        reason: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider = Provider()
        provider.claims = claims
        client, _ = await logged_in(provider)
        with caplog.at_level(logging.INFO, logger="fastapi_alcazaba.auth.strategies.base"):
            refused(provider, client)
        assert reason in caplog.text

    async def test_several_audiences_with_its_azp(self) -> None:
        provider = Provider()
        provider.claims = {"aud": [CLIENT_ID, "an-api"], "azp": CLIENT_ID}
        client, _ = await logged_in(provider)
        assert log_in(client, provider).status_code == 200

    async def test_without_at_hash(self) -> None:
        provider = Provider()
        provider.claims = {"at_hash": None}  # optional in the code flow
        client, _ = await logged_in(provider)
        assert log_in(client, provider).status_code == 200

    async def test_a_token_with_a_key_of_its_own(self, caplog: pytest.LogCaptureFixture) -> None:
        provider = Provider()
        provider.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        client, _ = await logged_in(provider)
        with caplog.at_level(logging.INFO, logger="fastapi_alcazaba.auth.strategies.base"):
            refused(provider, client)
        assert "InvalidSignatureError" in caplog.text

    @pytest.mark.parametrize("algorithm", ["none", "HS256"])
    async def test_algorithms_that_arent_accepted(
        self,
        algorithm: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider = Provider()
        payload = {"iss": ISSUER, "sub": "u", "aud": CLIENT_ID, "exp": 2**31, "iat": 0}
        key = None if algorithm == "none" else SECRET * 6  # the client secret, as an HMAC key
        provider.id_token = jwt.encode(payload, key, algorithm=algorithm)  # type: ignore[arg-type]
        client, _ = await logged_in(provider)
        with caplog.at_level(logging.INFO, logger="fastapi_alcazaba.auth.strategies.base"):
            refused(provider, client)
        assert f"algorithm isn't accepted: '{algorithm}'" in caplog.text

    async def test_no_id_token(self) -> None:
        provider = Provider()
        provider.id_token = ""
        client, _ = await logged_in(provider)
        refused(provider, client)

    @pytest.mark.parametrize("id_token", ["not-a-jwt", "x" * 20_000])
    async def test_garbage(self, id_token: str) -> None:
        provider = Provider()
        provider.id_token = id_token
        client, _ = await logged_in(provider)
        refused(provider, client)

    async def test_max_age(self) -> None:
        provider = Provider()
        provider.claims = {"auth_time": int(time.time()) - 3600}
        client, _ = await logged_in(provider, max_age=600)
        refused(provider, client)
        provider.claims = {"auth_time": int(time.time()) - 60}
        assert log_in(client, provider).status_code == 200
        provider.claims = {"auth_time": None}
        refused(provider, client)

    async def test_a_tampered_state_cookie(self) -> None:
        provider = Provider()
        client, _ = await logged_in(provider)
        client.get("/login")
        client.cookies.set("__Host-alcazaba-oauth2-idp", "nodots")
        response = client.get("/callback", params={"code": "the-code", "state": "nodots"})
        assert response.status_code == 401


class TestKeys:
    async def test_kept_then_fetched_again_for_a_new_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = Provider()
        client, _ = await logged_in(provider)
        for _ in range(3):
            assert log_in(client, provider).status_code == 200
        assert provider.fetched("/jwks") == 1
        # The provider rotates its key: the next token has a kid the app hasn't seen.
        rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        provider.keys = [provider.jwk(rotated, "rsa-2", "RS256")]
        provider.signing_key, provider.header = rotated, {"kid": "rsa-2"}
        monkeypatch.setattr(oidc_module, "JWKS_REFETCH", 0)
        assert log_in(client, provider).status_code == 200
        assert provider.fetched("/jwks") == 2

    async def test_unknown_keys_dont_refetch_on_every_request(self) -> None:
        provider = Provider()
        provider.header = {"kid": "made-up"}
        client, _ = await logged_in(provider)
        for _ in range(3):
            refused(provider, client)
        assert provider.fetched("/jwks") == 1

    async def test_a_key_without_kid_when_there_is_one(self) -> None:
        provider = Provider()
        provider.keys = provider.keys[:1]
        provider.header = {}
        client, _ = await logged_in(provider)
        assert log_in(client, provider).status_code == 200

    @pytest.mark.parametrize(
        ("jwks", "reason"),
        [
            (httpx.Response(500), "keys weren't read: HTTP 500"),
            (httpx.Response(200, json={"keys": []}), "aren't a JWKS"),
        ],
    )
    async def test_keys_that_cant_be_read(
        self,
        jwks: httpx.Response,
        reason: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider = Provider()
        handler = provider.handler
        provider.handler = lambda request: jwks if request.url.path == "/jwks" else handler(request)  # type: ignore[method-assign]
        client, _ = await logged_in(provider)
        with caplog.at_level(logging.INFO, logger="fastapi_alcazaba.auth.strategies.base"):
            refused(provider, client)
        assert reason in caplog.text

    async def test_made_up_kids_dont_make_a_fetch_each(self) -> None:
        """Anyone can call the back-channel logout endpoint: tokens with made-up kids, while the
        provider is down, make one fetch every 30 seconds, not one each, and the keys kept are
        used meanwhile."""
        provider = Provider()
        provider.nonce = "the-login-nonce"
        oidc = strategy(provider)
        await oidc.discover()
        assert (await oidc.verify_id_token(provider.sign(), nonce=provider.nonce))[
            "sub"
        ] == "user-1"
        assert provider.fetched("/jwks") == 1
        provider.keys = []  # down: an empty JWKS
        oidc._keys_fetched -= 3600 + 1  # the keys are older than jwks_lifespan
        oidc._keys_attempted -= 3600 + 1
        for number in range(20):
            forged = jwt.encode({"sub": "x"}, provider.rsa, "RS256", headers={"kid": f"k{number}"})
            with pytest.raises(AuthenticationFailed, match="isn't the provider's"):
                await oidc.verify_logout_token(forged)
        assert provider.fetched("/jwks") == 2
        # The kept key still verifies, as a stale one.
        assert (await oidc.verify_id_token(provider.sign(), nonce=provider.nonce))[
            "sub"
        ] == "user-1"
        oidc._keys_fetched -= 3600  # and past jwks_stale too: refused
        with pytest.raises(AuthenticationFailed, match="aren't a JWKS"):
            await oidc.verify_id_token(provider.sign(), nonce=provider.nonce)

    async def test_a_kid_that_isnt_a_string(self) -> None:
        provider = Provider()
        signed = provider.sign().split(".")
        header = base64.urlsafe_b64encode(b'{"alg":"RS256","kid":7}').decode().rstrip("=")
        provider.id_token = ".".join([header, *signed[1:]])  # PyJWT won't make one
        client, _ = await logged_in(provider)
        refused(provider, client)

    async def test_encryption_keys_dont_sign(self) -> None:
        provider = Provider()
        provider.keys = [{**provider.keys[0], "use": "enc"}]
        client, _ = await logged_in(provider)
        refused(provider, client)


class TestDiscovery:
    async def test_endpoints(self) -> None:
        oidc = strategy(Provider(), userinfo=True)
        await oidc.discover()
        assert (oidc.authorization_url, oidc.token_url, oidc.userinfo_url, oidc.jwks_url) == (
            f"{ISSUER}/authorize",
            f"{ISSUER}/token",
            f"{ISSUER}/userinfo",
            f"{ISSUER}/jwks",
        )

    async def test_another_issuer_is_refused(self) -> None:
        provider = Provider()
        provider.issuer = "https://evil.example.com"
        with pytest.raises(RuntimeError, match="issuer is 'https://evil.example.com'"):
            await strategy(provider).discover()

    async def test_no_document(self) -> None:
        oidc = strategy(Provider(), issuer="https://idp.example.com/realms/none")
        with pytest.raises(RuntimeError, match="answered 404"):
            await oidc.discover()

    async def test_given_endpoints_win(self) -> None:
        oidc = strategy(Provider(), token_url=PROXY)
        await oidc.discover()
        assert oidc.token_url == PROXY

    async def test_unreachable(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        oidc = OIDCStrategy(
            name="idp",
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=SECRET,
            redirect_uri="https://testserver/callback",
            client=httpx.AsyncClient(transport=httpx.MockTransport(down)),
        )
        with pytest.raises(RuntimeError, match="didn't answer"):
            await oidc.discover()

    @pytest.mark.parametrize(
        ("document", "message"),
        [
            ({"issuer": ISSUER, "authorization_endpoint": "http://idp.example.com/a"}, "HTTPS"),
            ({"issuer": ISSUER, "token_endpoint": f"{ISSUER}/t"}, "no authorization_url"),
        ],
    )
    async def test_documents_that_dont_fit(self, document: dict[str, Any], message: str) -> None:
        oidc = OIDCStrategy(
            name="idp",
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=SECRET,
            redirect_uri="https://testserver/callback",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=document))
            ),
        )
        with pytest.raises(RuntimeError, match=message):
            await oidc.discover()

    async def test_userinfo_needs_its_endpoint(self) -> None:
        document = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/a",
            "token_endpoint": f"{ISSUER}/t",
            "jwks_uri": f"{ISSUER}/k",
        }
        oidc = strategy(Provider(), userinfo=True)
        oidc.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=document))
        )
        with pytest.raises(RuntimeError, match="no endpoint"):
            await oidc.discover()

    def test_not_discovered_yet(self) -> None:
        with pytest.raises(RuntimeError, match="isn't known yet"):
            app_of(strategy(Provider())).get("/login")


class TestConfiguration:
    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"scopes": ["email"]}, "openid scope"),
            ({"algorithms": ["HS256"]}, "asymmetric"),
            ({"algorithms": []}, "asymmetric"),
            ({"max_age": -1}, "max_age"),
            ({"params": {"nonce": "fixed"}}, "nonce is the strategy's"),
            ({"issuer": "http://idp.example.com"}, "HTTPS"),
            ({"jwks_url": "http://idp.example.com/jwks"}, "HTTPS"),
        ],
    )
    def test_invalid(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            strategy(Provider(), **options)
