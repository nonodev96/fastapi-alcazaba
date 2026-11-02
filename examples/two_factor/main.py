"""A second factor: an authenticator app's codes after the password, recovery codes for a lost
phone, and a recent code to delete the account.

    uvicorn examples.two_factor.main:app

Ana logs in with her password ("correct horse battery staple"), then adds an authenticator app:
`/totp/enroll` gives the `otpauth://` URI to show as a QR code, and `/totp/confirm` checks the
first code it shows and gives the recovery codes, once. From then on, her logins need a code of
the app (`/login/totp`) or a recovery code (`/login/recovery`), and deleting her account a code
of the last five minutes.

Set TOTP_SECRET and CACHE_SIGNING_KEY (`secrets.token_urlsafe(32)` each), the same in every
process, in the environment or a .env file.
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

from fastapi_alcazaba import FastAPIAudit, FastAPICache, FastAPITOTP
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import (
    CSRFMiddleware,
    ServerSessionMiddleware,
    clear_session,
    rotate_session,
)

DEMO_PASSWORD = "correct horse battery staple"  # noqa: S105  # Ana's, to try the example


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes (the TOTP secrets sealed before a restart don't open after it)."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    totp_secret: SecretStr = Field(default_factory=throwaway_secret)
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


def password_digest(password: str, salt: bytes) -> bytes:
    """scrypt: slow on purpose, so that a stolen database gives the passwords away slowly."""
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


class User(BaseModel):
    id: str
    username: str
    salt: bytes
    password: bytes
    totp: str | None = None  # the sealed secret: nothing in clear
    recovery: list[str] = []  # the recovery codes' digests


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        salt = secrets.token_bytes(16)
        digest = password_digest(DEMO_PASSWORD, salt)
        ana = User(id="u1", username="ana", salt=salt, password=digest)
        self.users = {ana.id: ana}

    def by_name(self, username: str) -> User | None:
        return next((user for user in self.users.values() if user.username == username), None)


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class Code(BaseModel):
    code: str = Field(max_length=20)


db = Database()
audit = FastAPIAudit()  # the second factors checked, to the audit log
totp = FastAPITOTP(issuer="Example", secret=settings.totp_secret, audit=audit)
# A password checked against a made-up digest when the user doesn't exist: the same time either way.
UNKNOWN = User(id="", username="", salt=b"\0" * 16, password=password_digest("x", b"\0" * 16))


def current_user(request: Request) -> User:
    """The user logged in with both factors; a 401 otherwise."""
    user = db.users.get(request.session.get("user_id", ""))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    return user


def current_user_id(request: Request) -> str | None:
    return request.session.get("user_id")


CurrentUser = Annotated[User, Depends(current_user)]


def log_in(request: Request, user: User) -> dict[str, str]:
    request.session.pop("pending", None)
    request.session["user_id"] = user.id
    rotate_session(request)  # no session fixation
    return {"logged_in": user.username}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production: the codes used and the guesses, shared by every process.
    FastAPICache.init(InMemoryBackend(), prefix="mfa", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(ServerSessionMiddleware, max_age=8 * 3600, idle_timeout=1800)
app.add_middleware(CSRFMiddleware)  # session cookies: refuse what other sites make browsers send


@app.post("/login")
async def login(request: Request, form: Login) -> dict[str, str]:
    """The password, then the second factor when the user has one: until then, the session only
    knows who's halfway in."""
    user = db.by_name(form.username)
    found = user or UNKNOWN
    right = hmac.compare_digest(password_digest(form.password, found.salt), found.password)
    if not right or user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password")
    if user.totp is None:
        return log_in(request, user)
    request.session["pending"] = user.id
    return {"next": "/login/totp"}


@app.post("/login/totp")
async def login_totp(request: Request, form: Code) -> dict[str, str]:
    """A code of the user's app: the login is complete, and the second factor recent."""
    user = db.users.get(request.session.get("pending", ""))
    if user is None or user.totp is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Log in with your password first")
    if not await totp.verify(user.id, user.totp, form.code, request=request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong code")
    logged_in = log_in(request, user)
    totp.mark(request, user.id)
    return logged_in


@app.post("/login/recovery")
async def login_recovery(request: Request, form: Code) -> dict[str, str | int]:
    """A recovery code instead of the app's: used up, and the user told how many are left."""
    user = db.users.get(request.session.get("pending", ""))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Log in with your password first")
    used = await totp.use_recovery_code(user.id, form.code, user.recovery, request=request)
    if used is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong code")
    user.recovery.remove(used)
    log_in(request, user)
    totp.mark(request, user.id)
    return {"logged_in": user.username, "recovery_codes_left": len(user.recovery)}


@app.post("/totp/enroll")
async def enroll(request: Request, user: CurrentUser) -> dict[str, str]:
    """A new secret for the user's app: its URI as a QR code, or the secret to type. Kept
    sealed in the session until a code of it is confirmed."""
    enrollment = totp.enroll(user.id, label=user.username)
    request.session["enrolling"] = enrollment.sealed
    return {"uri": enrollment.uri, "secret": enrollment.secret.get_secret_value()}


@app.post("/totp/confirm")
async def confirm(request: Request, form: Code, user: CurrentUser) -> dict[str, list[str]]:
    """The first code of the app: the second factor is on, and the recovery codes shown once."""
    sealed = request.session.get("enrolling")
    if sealed is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Enroll first")
    if not await totp.verify(user.id, sealed, form.code, request=request):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Wrong code: try the next one")
    recovery = totp.recovery_codes(user.id)
    user.totp, user.recovery = sealed, recovery.digests
    del request.session["enrolling"]
    totp.mark(request, user.id)
    return {"recovery_codes": recovery.codes}


@app.get("/me")
async def me(user: CurrentUser) -> dict[str, str | bool]:
    return {"username": user.username, "second_factor": user.totp is not None}


@app.delete(
    "/account",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(totp.require_recent(current_user_id, max_age=300))],
)
async def delete_account(request: Request, user: CurrentUser) -> None:
    """Needs a code of the last five minutes: a session left open isn't enough."""
    del db.users[user.id]
    clear_session(request)


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
