"""An API for scripts and apps: they log in with a password and get a JWT, which they send in
`Authorization: Bearer` for 15 minutes, and a refresh token, which gets them a new pair when it
expires, until they log out.

    uvicorn examples.auth_strategies.jwt_tokens:app

`JWTStrategy` verifies the tokens the app issues: their signature, their expiry, who issued
them and for whom (`issuer`, `audience`), and that they weren't revoked (`TokenBlocklist`). The
token carries the user's id, so the app knows who sends it without looking the token up.
`RefreshTokens` rotates the refresh tokens: each one is used up when it's exchanged, and one used
twice (it was copied) ends the login.

Log in as octocat, with the password "correct horse battery staple". Set JWT_SECRET,
REFRESH_SECRET and CACHE_SIGNING_KEY (secrets.token_urlsafe(32) each), the same in every process,
in the environment or a .env file.
"""

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPIAuth, FastAPICache
from fastapi_alcazaba.auth import RefreshRefused, RefreshTokens, TokenBlocklist, TokenPair
from fastapi_alcazaba.auth.strategies.jwt import JWTStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter

TOKEN_LIFETIME = 900  # seconds: the refresh token gets a new one
DEMO_PASSWORD = "correct horse battery staple"  # noqa: S105  # octocat's, to try the example


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    base_url: str = "http://localhost:8000"
    jwt_secret: SecretStr = Field(default_factory=throwaway_secret)
    refresh_secret: SecretStr = Field(default_factory=throwaway_secret)
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class User(BaseModel):
    id: str
    login: str
    name: str | None = None


def password_digest(password: str, salt: bytes) -> bytes:
    """scrypt: slow on purpose, so that a stolen database gives the passwords away slowly."""
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


# What unknown logins are checked against, so that they take as long as wrong passwords.
UNKNOWN_SALT = secrets.token_bytes(16)
UNKNOWN_DIGEST = password_digest(secrets.token_urlsafe(), UNKNOWN_SALT)


class Database:
    """Stands in for the database: one user, octocat, with the demo password."""

    def __init__(self) -> None:
        self.users = {"1": User(id="1", login="octocat", name="The Octocat")}
        salt = secrets.token_bytes(16)
        self.passwords = {"octocat": ("1", salt, password_digest(DEMO_PASSWORD, salt))}

    def check_password(self, login: str, password: str) -> User | None:
        user_id, salt, digest = self.passwords.get(login, ("", UNKNOWN_SALT, UNKNOWN_DIGEST))
        if hmac.compare_digest(password_digest(password, salt), digest) and user_id:
            return self.users[user_id]
        return None


db = Database()

blocklist = TokenBlocklist()  # in FastAPICache's backend
auth = FastAPIAuth()
tokens = auth.use(
    JWTStrategy(
        settings.jwt_secret,
        issuer=settings.base_url,  # tokens of this app
        audience=f"{settings.base_url}/api",  # for this API: not another one's tokens
        blocklist=blocklist,  # revoked tokens are refused
        verify=lambda claims: db.users.get(claims["sub"]),
    )
)
# A login lasts 30 days, or 7 without a refresh; a refresh token used twice ends it.
refresh_tokens = RefreshTokens(tokens, secret=settings.refresh_secret, access_ttl=TOKEN_LIFETIME)
CurrentUser = Annotated[User, Depends(auth.authenticate("jwt"))]
# Five logins a minute per address: a script that guesses passwords soon gets 429s.
login_attempts = FastAPIRateLimiter(times=5, minutes=1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process sees the logins and revoked tokens.
    FastAPICache.init(InMemoryBackend(), prefix="jwt", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)


class Login(BaseModel):
    login: str = Field(max_length=100)
    password: str = Field(max_length=200)


class Refresh(BaseModel):
    refresh_token: str = Field(max_length=200)


@app.post("/token", dependencies=[Depends(login_attempts)])
async def log_in(form: Login) -> TokenPair:
    """Tokens for the user's scripts, for their login and password."""
    user = db.check_password(form.login, form.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong login or password")
    return await refresh_tokens.issue(user.id)


@app.post("/token/refresh")
async def refresh(body: Refresh) -> TokenPair:
    """A new pair for the refresh token, which is used up."""
    try:
        return await refresh_tokens.exchange(body.refresh_token)
    except RefreshRefused:
        # Never say why: a thief would learn which of its tokens still work.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Log in again") from None


@app.get("/me")
async def me(user: CurrentUser) -> User:
    return user


@app.delete("/token", status_code=status.HTTP_204_NO_CONTENT)
async def log_out(
    user: CurrentUser,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> None:
    """End the login of the request's token: its access and refresh tokens are refused."""
    del user  # checked by the JWT strategy: valid and not revoked
    claims = await tokens.decode(credentials.credentials)
    await refresh_tokens.revoke_session(claims["sid"])


if __name__ == "__main__":
    uvicorn.run("jwt_tokens:app", reload=True)
