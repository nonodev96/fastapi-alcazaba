"""People log in with GitHub; their scripts, with tokens they create: authentication strategies.

    uvicorn examples.auth_strategies.main:app

Three strategies, each a way to tell who makes a request: `OAuth2Strategy` logs people in with
their GitHub account, `SessionStrategy` remembers them in a server-side session afterwards, and
`JWTStrategy` reads the tokens they create for their scripts. `/me` takes any of the last two:
`auth.authenticate("session", "jwt")` tries them in order.

Create a GitHub OAuth app (Settings → Developer settings → OAuth Apps) whose callback URL is
http://localhost:8000/auth/github/callback, and set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET.
Set JWT_SECRET and CACHE_SIGNING_KEY too (secrets.token_urlsafe(32) each), the same in every
process, in the environment or a .env file. Open the app at http://localhost:8000: GitHub only
sends people back to the callback URL it knows.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPIAuth, FastAPICache
from fastapi_alcazaba.auth import SessionStrategy, TokenBlocklist
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy
from fastapi_alcazaba.auth.strategies.oauth2 import OAuth2Strategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import CSRFMiddleware, ServerSessionMiddleware

TOKEN_LIFETIME = 3600  # seconds


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    base_url: str = "http://localhost:8000"
    github_client_id: str = "set GITHUB_CLIENT_ID"
    github_client_secret: SecretStr = SecretStr("set GITHUB_CLIENT_SECRET")
    jwt_secret: SecretStr = Field(default_factory=throwaway_secret)
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class User(BaseModel):
    id: str
    login: str
    name: str | None = None


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}


db = Database()


def find_or_create_user(tokens: dict[str, Any], profile: dict[str, Any] | None) -> User | None:
    """GitHub says who logged in: our user for that account, created the first time."""
    del tokens  # GitHub's access token: keep it to call GitHub on the user's behalf
    if profile is None:
        return None  # refuses the login
    user_id = f"github:{profile['id']}"
    if user_id not in db.users:
        db.users[user_id] = User(id=user_id, login=profile["login"], name=profile.get("name"))
    return db.users[user_id]


def load_user(user_id: str) -> User | None:
    return db.users.get(user_id)


blocklist = TokenBlocklist()  # in FastAPICache's backend
auth = FastAPIAuth()
github = auth.use(
    OAuth2Strategy(
        name="github",
        authorization_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",  # noqa: S106  # a URL
        userinfo_url="https://api.github.com/user",
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        redirect_uri=f"{settings.base_url}/auth/github/callback",
        scopes=["read:user"],
        verify=find_or_create_user,
    )
)
sessions = auth.use(SessionStrategy(load_user))
api_tokens = auth.use(
    JWTStrategy(
        settings.jwt_secret,
        issuer=settings.base_url,
        blocklist=blocklist,  # revoked tokens are refused
        verify=lambda claims: load_user(claims["sub"]),
    )
)

# Who makes the request, by each way the routes accept.
GitHubUser = Annotated[User, Depends(auth.authenticate("github"))]
SessionUser = Annotated[User, Depends(auth.authenticate("session"))]
TokenUser = Annotated[User, Depends(auth.authenticate("jwt"))]
AnyUser = Annotated[User, Depends(auth.authenticate("session", "jwt"))]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares sessions and revocations.
    FastAPICache.init(InMemoryBackend(), prefix="auth", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(ServerSessionMiddleware)
app.add_middleware(CSRFMiddleware)  # session cookies: refuse what other sites make browsers send


@app.get("/login/github")
async def login() -> RedirectResponse:
    """To GitHub's login page, which sends the user back to the callback."""
    return github.redirect()


@app.get("/auth/github/callback")
async def github_callback(request: Request, user: GitHubUser) -> RedirectResponse:
    """GitHub sends the user back here: the strategy checks the state, exchanges the code and
    reads the profile; the session remembers the user from now on."""
    sessions.login(request, user.id)
    return RedirectResponse("/me", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, user: SessionUser) -> None:
    del user  # only to refuse requests without a session
    sessions.logout(request)


@app.get("/me")
async def me(user: AnyUser) -> User:
    """With the session (a browser) or a token (a script)."""
    return user


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105  # the type of token, not one
    expires_in: int = TOKEN_LIFETIME


@app.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_token(user: SessionUser) -> Token:
    """A token for the user's scripts. Only with the session: a token can't make tokens."""
    return Token(access_token=api_tokens.issue(user.id, expires_in=TOKEN_LIFETIME))


@app.delete("/tokens/current", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    user: TokenUser,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> None:
    """Refuse the request's token from now on, until it would have expired anyway."""
    del user  # checked by the JWT strategy: valid and not revoked
    claims = await api_tokens.decode(credentials.credentials)
    await blocklist.revoke(claims["jti"], expires_at=claims["exp"])


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
