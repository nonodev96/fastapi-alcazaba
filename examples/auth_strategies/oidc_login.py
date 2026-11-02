"""Log in with an OpenID Connect provider (Keycloak, Google, Microsoft Entra ID, Okta, Zitadel...):
the ID token verified, then a session.

    uvicorn examples.auth_strategies.oidc_login:app

`/login` sends the browser to the provider, which sends the user back to the callback. There
`OIDCStrategy` checks the state, exchanges the code, verifies the ID token (its signature with
the provider's keys, its issuer, audience, expiry and the login's nonce) and calls
`find_or_create_user` with its claims; the app keeps its own user in a session
(`SessionStrategy`), so the provider isn't asked again on every request. The provider's
endpoints and keys come from its issuer's discovery document, read at start.

Register the app at the provider with the redirect URI http://localhost:8000/auth/callback, and
set OIDC_ISSUER (Keycloak's is http://localhost:8080/realms/<realm>), OIDC_CLIENT_ID and
OIDC_CLIENT_SECRET. Set CACHE_SIGNING_KEY too (secrets.token_urlsafe(32)), the same in every
process, in the environment or a .env file. Open the app at http://localhost:8000: browsers take
its Secure cookies from localhost.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPIAuth, FastAPICache
from fastapi_alcazaba.auth import SessionStrategy
from fastapi_alcazaba.auth.strategies.oidc import OIDCStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import CSRFMiddleware, ServerSessionMiddleware


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    base_url: str = "http://localhost:8000"
    oidc_issuer: str = "http://localhost:8080/realms/example"
    oidc_client_id: str = "set OIDC_CLIENT_ID"
    oidc_client_secret: SecretStr = SecretStr("set OIDC_CLIENT_SECRET")
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class User(BaseModel):
    id: str
    email: str | None = None
    name: str | None = None


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}


db = Database()


def find_or_create_user(claims: dict[str, Any], userinfo: dict[str, Any] | None) -> User | None:
    """The ID token says who logged in: our user for that account, created the first time. Its
    claims are verified: they come from the provider, for this app, for this login."""
    del userinfo  # userinfo=False: the ID token's claims are enough here
    if claims.get("email") and claims.get("email_verified") is False:
        return None  # an address nobody proved theirs: refuse the login
    # `sub` is unique and stable per issuer; the e-mail address may change hands.
    user_id = f"{claims['iss']}#{claims['sub']}"
    if user_id not in db.users:
        db.users[user_id] = User(id=user_id, email=claims.get("email"), name=claims.get("name"))
    return db.users[user_id]


def load_user(user_id: str) -> User | None:
    return db.users.get(user_id)


auth = FastAPIAuth()
provider = auth.use(
    OIDCStrategy(
        name="oidc",
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=f"{settings.base_url}/auth/callback",
        verify=find_or_create_user,
    )
)
sessions = auth.use(SessionStrategy(load_user))
LoggedIn = Annotated[User, Depends(auth.authenticate("oidc"))]
CurrentUser = Annotated[User, Depends(auth.authenticate("session"))]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # The provider's endpoints and keys, from its issuer: the app doesn't start without them.
    await provider.discover()
    # Redis or Valkey in production, so that every process shares the sessions.
    FastAPICache.init(InMemoryBackend(), prefix="oidc", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(ServerSessionMiddleware)
app.add_middleware(CSRFMiddleware)  # session cookies: refuse what other sites make browsers send


@app.get("/login")
async def log_in() -> RedirectResponse:
    """To the provider's login page, which sends the user back to the callback."""
    return provider.redirect()


@app.get("/auth/callback")
async def callback(request: Request, user: LoggedIn) -> RedirectResponse:
    """The provider sends the user back here: the strategy checks the state, exchanges the code
    and verifies the ID token; the session remembers the user from now on."""
    sessions.login(request, user.id)
    return RedirectResponse("/me", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/me")
async def me(user: CurrentUser) -> User:
    return user


@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def log_out(request: Request, user: CurrentUser) -> None:
    del user  # only to refuse requests without a session
    sessions.logout(request)


if __name__ == "__main__":
    uvicorn.run("oidc_login:app", reload=True)
