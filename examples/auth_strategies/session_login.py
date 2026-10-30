"""A web app's login: a password, then a server-side session that remembers who logged in.

    uvicorn examples.auth_strategies.session_login:app

`sessions.login()` keeps the user's id in a session stored in the cache's backend, under a new
id (so that one planted in the browser before doesn't get logged in too); the browser only
holds that id, in a cookie. `SessionStrategy` loads the user from it on each request, and
`sessions.logout()` deletes the session from the backend.

Log in as octocat, with the password "correct horse battery staple". Set CACHE_SIGNING_KEY
(secrets.token_urlsafe(32)), the same in every process, in the environment or a .env file.
"""

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPIAuth, FastAPICache
from fastapi_alcazaba.auth import SessionStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter
from fastapi_alcazaba.security import CSRFMiddleware, ServerSessionMiddleware

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


def load_user(user_id: str) -> User | None:
    return db.users.get(user_id)


auth = FastAPIAuth()
sessions = auth.use(SessionStrategy(load_user))
CurrentUser = Annotated[User, Depends(auth.authenticate("session"))]
# Five logins a minute per address: guessing passwords soon gets 429s.
login_attempts = FastAPIRateLimiter(times=5, minutes=1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the sessions.
    FastAPICache.init(InMemoryBackend(), prefix="web", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(ServerSessionMiddleware)
app.add_middleware(CSRFMiddleware)  # session cookies: refuse what other sites make browsers send


class Login(BaseModel):
    login: str = Field(max_length=100)
    password: str = Field(max_length=200)


@app.post("/login", dependencies=[Depends(login_attempts)])
async def log_in(request: Request, form: Login) -> User:
    """Start a session for the user of that login and password."""
    user = db.check_password(form.login, form.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong login or password")
    sessions.login(request, user.id)  # a new session id: no session fixation
    return user


@app.get("/me")
async def me(user: CurrentUser) -> User:
    return user


@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def log_out(request: Request, user: CurrentUser) -> None:
    del user  # only to refuse requests without a session
    sessions.logout(request)


if __name__ == "__main__":
    uvicorn.run("session_login:app", reload=True)
