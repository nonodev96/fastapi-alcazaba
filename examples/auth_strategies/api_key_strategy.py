"""A strategy of your own: API keys in `X-API-Key`, for the programs that call the API.

    uvicorn examples.auth_strategies.api_key_strategy:app

A strategy is a class whose `authenticate()` returns the user; None when the request doesn't
carry its credentials, so that the next strategy is tried; or raises AuthenticationFailed when
they're there but wrong. This one hands the key to FastAPIAPIKeys, which keeps keys as digests
bound to a server-side pepper, and gives FastAPIAuth the key's owner. As a strategy it goes
with the others: `auth.authenticate("api-key", "jwt")` would take a key or a JWT.

Create a key as octocat, with the password "correct horse battery staple". Set API_KEY_PEPPER
and CACHE_SIGNING_KEY (secrets.token_urlsafe(32) each), the same in every process, in the
environment or a .env file.
"""

import dataclasses
import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import HTTPConnection

from fastapi_alcazaba import FastAPIAPIKeys, FastAPIAuth, FastAPICache
from fastapi_alcazaba.auth import AuthenticationFailed, Strategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter

DEMO_PASSWORD = "correct horse battery staple"  # noqa: S105  # octocat's, to try the example


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_key_pepper: SecretStr = Field(default_factory=throwaway_secret)
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class User(BaseModel):
    id: str
    login: str
    name: str | None = None


@dataclasses.dataclass
class APIKeyRow:
    """A row of the api_keys table: everything about a key but the key itself."""

    id: str
    owner: str
    digest: str
    scopes: frozenset[str]
    expires_at: datetime | None


def password_digest(password: str, salt: bytes) -> bytes:
    """scrypt: slow on purpose, so that a stolen database gives the passwords away slowly."""
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


# What unknown logins are checked against, so that they take as long as wrong passwords.
UNKNOWN_SALT = secrets.token_bytes(16)
UNKNOWN_DIGEST = password_digest(secrets.token_urlsafe(), UNKNOWN_SALT)


class Database:
    """Stands in for the database: one user, octocat, with the demo password, and their keys."""

    def __init__(self) -> None:
        self.users = {"1": User(id="1", login="octocat", name="The Octocat")}
        salt = secrets.token_bytes(16)
        self.passwords = {"octocat": ("1", salt, password_digest(DEMO_PASSWORD, salt))}
        self.api_keys: dict[str, APIKeyRow] = {}

    def check_password(self, login: str, password: str) -> User | None:
        user_id, salt, digest = self.passwords.get(login, ("", UNKNOWN_SALT, UNKNOWN_DIGEST))
        if hmac.compare_digest(password_digest(password, salt), digest) and user_id:
            return self.users[user_id]
        return None


db = Database()


async def load_key(key_id: str) -> APIKeyRow | None:
    return db.api_keys.get(key_id)


keys = FastAPIAPIKeys(load_key, pepper=settings.api_key_pepper, prefix="demo")


class APIKeyStrategy(Strategy):
    """The owner of the key in `X-API-Key`."""

    challenge = "APIKey"  # the WWW-Authenticate of the 401s

    def __init__(self, api_keys: FastAPIAPIKeys[APIKeyRow]) -> None:
        super().__init__("api-key")
        self.api_keys = api_keys

    def openapi_scheme(self) -> APIKeyHeader:
        """For the docs' Authorize button."""
        return APIKeyHeader(name="X-API-Key", scheme_name=self.name, auto_error=False)

    async def authenticate(self, connection: HTTPConnection) -> User | None:
        key = connection.headers.get("x-api-key")
        if key is None:
            return None  # not this strategy's: the next one is tried
        try:
            row = await self.api_keys.authenticate(key)
        except HTTPException as exc:  # FastAPIAPIKeys' 401: unknown, wrong or expired
            raise AuthenticationFailed("invalid API key") from exc
        owner = db.users.get(row.owner)
        if owner is None:
            raise AuthenticationFailed("the key's owner is gone")
        return owner


auth = FastAPIAuth()
auth.use(APIKeyStrategy(keys))
CurrentUser = Annotated[User, Depends(auth.authenticate("api-key"))]
# Five tries a minute per address: guessing passwords soon gets 429s.
login_attempts = FastAPIRateLimiter(times=5, minutes=1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the limit's counters.
    FastAPICache.init(InMemoryBackend(), prefix="keys", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)


class Login(BaseModel):
    login: str = Field(max_length=100)
    password: str = Field(max_length=200)


class NewKey(BaseModel):
    id: str
    key: str
    expires_at: datetime


@app.post("/api-keys", status_code=status.HTTP_201_CREATED, dependencies=[Depends(login_attempts)])
async def create_key(form: Login) -> NewKey:
    """A new key, valid for 90 days and shown this once: only a digest of it is stored."""
    user = db.check_password(form.login, form.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong login or password")
    expires_at = datetime.now(UTC) + timedelta(days=90)
    key, record = keys.generate(expires_at=expires_at)
    db.api_keys[record.id] = APIKeyRow(owner=user.id, **dataclasses.asdict(record))
    return NewKey(id=record.id, key=key, expires_at=expires_at)


@app.get("/me")
async def me(user: CurrentUser) -> User:
    return user


if __name__ == "__main__":
    uvicorn.run("api_key_strategy:app", reload=True)
