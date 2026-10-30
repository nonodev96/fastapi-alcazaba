"""Personal access tokens: random strings that users create for their scripts, kept as digests,
and refused as soon as they're deleted.

    uvicorn examples.auth_strategies.bearer_tokens:app

`HTTPBearerStrategy` hands the token of `Authorization: Bearer` to `find_token`, which looks its
digest up in the database. Unlike a JWT, the token says nothing by itself: deleting its row
refuses it at once, in every process, and a stolen database holds no token that works.

Create a token as octocat, with the password "correct horse battery staple". Set
CACHE_SIGNING_KEY (secrets.token_urlsafe(32)), the same in every process, in the environment or a
.env file.
"""

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPIAuth, FastAPICache
from fastapi_alcazaba.auth import HTTPBearerStrategy
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

    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class User(BaseModel):
    id: str
    login: str
    name: str | None = None


def password_digest(password: str, salt: bytes) -> bytes:
    """scrypt: slow on purpose, so that a stolen database gives the passwords away slowly."""
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


def token_digest(token: str) -> str:
    """What the database keeps of a token. Tokens are random and long, so a plain SHA-256 is
    enough: there's nothing to guess."""
    return hashlib.sha256(token.encode()).hexdigest()


# What unknown logins are checked against, so that they take as long as wrong passwords.
UNKNOWN_SALT = secrets.token_bytes(16)
UNKNOWN_DIGEST = password_digest(secrets.token_urlsafe(), UNKNOWN_SALT)


class Database:
    """Stands in for the database: one user, octocat, with the demo password, and the digests of
    their tokens."""

    def __init__(self) -> None:
        self.users = {"1": User(id="1", login="octocat", name="The Octocat")}
        salt = secrets.token_bytes(16)
        self.passwords = {"octocat": ("1", salt, password_digest(DEMO_PASSWORD, salt))}
        self.tokens: dict[str, str] = {}  # the digest of each token: its user's id

    def check_password(self, login: str, password: str) -> User | None:
        user_id, salt, digest = self.passwords.get(login, ("", UNKNOWN_SALT, UNKNOWN_DIGEST))
        if hmac.compare_digest(password_digest(password, salt), digest) and user_id:
            return self.users[user_id]
        return None


db = Database()


async def find_token(token: str) -> User | None:
    """The user of the token; None when it's unknown or was deleted (a 401)."""
    user_id = db.tokens.get(token_digest(token))
    return db.users.get(user_id) if user_id is not None else None


auth = FastAPIAuth()
auth.use(HTTPBearerStrategy(find_token))
CurrentUser = Annotated[User, Depends(auth.authenticate("bearer"))]
# Five tries a minute per address: guessing passwords soon gets 429s.
login_attempts = FastAPIRateLimiter(times=5, minutes=1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the limit's counters.
    FastAPICache.init(InMemoryBackend(), prefix="pat", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)


class Login(BaseModel):
    login: str = Field(max_length=100)
    password: str = Field(max_length=200)


class NewToken(BaseModel):
    token: str
    created_at: datetime


@app.post("/tokens", status_code=status.HTTP_201_CREATED, dependencies=[Depends(login_attempts)])
async def create_token(form: Login) -> NewToken:
    """A new token, shown this once: only its digest is kept."""
    user = db.check_password(form.login, form.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong login or password")
    token = f"pat_{secrets.token_urlsafe(32)}"  # a prefix that secret scanners can spot
    db.tokens[token_digest(token)] = user.id
    return NewToken(token=token, created_at=datetime.now(UTC))


@app.get("/me")
async def me(user: CurrentUser) -> User:
    return user


@app.delete("/tokens/current", status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(
    user: CurrentUser,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> None:
    """Delete the request's token: refused from the next request on."""
    del user  # found by the strategy
    db.tokens.pop(token_digest(credentials.credentials), None)


if __name__ == "__main__":
    uvicorn.run("bearer_tokens:app", reload=True)
