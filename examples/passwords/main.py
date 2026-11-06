"""Accounts with a password: weak, common and leaked ones refused at sign-up and at every change.

    uvicorn examples.passwords.main:app

- `POST /signup`: an account. Its password is evaluated with the person's e-mail address and
  name: too short, made of them, one of the app's words, easy to guess for zxcvbn, or among the
  leaked ones of Have I Been Pwned, it's a 422 with the reasons and advice.
- `POST /login`: the e-mail address and password; guesses limited per account.
- `POST /password/change`: the current password, then a new one, evaluated as at sign-up.

Have I Been Pwned is asked by k-anonymity (the first five characters of the password's SHA-1);
when it doesn't answer in two seconds, the password is accepted and the result says so
(`fail_open`). Needs the passwords and password-strength extras. Set CACHE_SIGNING_KEY and
PASSWORD_PEPPER (secrets.token_urlsafe(32) each), the same in every process.
"""

import dataclasses
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.auth import (
    FastAPIPasswords,
    PasswordBlocklist,
    PasswordStrength,
    PwnedPasswords,
)
from fastapi_alcazaba.auth.passwords import MAX_PASSWORD
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.core.errors import BackendUnavailable


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)
    password_pepper: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()

passwords = FastAPIPasswords(
    pepper=settings.password_pepper,
    # zxcvbn: 3, "safely unguessable", is its own advice for an account that matters.
    strength=PasswordStrength(min_score=3),
    # The app's own words, which no list of common passwords has. A file of them (SecLists'
    # top 100 000) would be PasswordBlocklist.from_files("common.txt"), loaded once, here.
    blocklist=PasswordBlocklist(["Lighthouse", "Lighthouse Notes", "lighthousenotes"]),
    pwned=PwnedPasswords(timeout=2.0, fail_open=True),
)


@dataclasses.dataclass
class User:
    id: int
    email: str
    name: str
    password: str  # the hash


users: dict[str, User] = {}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production: the wrong guesses are counted for every process.
    FastAPICache.init(InMemoryBackend(), prefix="passwords", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(title="Lighthouse Notes", lifespan=lifespan)


@app.exception_handler(BackendUnavailable)
async def unavailable(_: Request, __: BackendUnavailable) -> JSONResponse:
    """With PwnedPasswords(fail_open=False), a password that couldn't be checked: try again."""
    return JSONResponse({"detail": "Try again in a moment"}, status.HTTP_503_SERVICE_UNAVAILABLE)


# SecretStr: the passwords stay out of reprs, logs and tracebacks.
class SignUp(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    name: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(max_length=MAX_PASSWORD)


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: SecretStr = Field(max_length=MAX_PASSWORD)


class PasswordChange(BaseModel):
    email: str = Field(max_length=254)
    current_password: SecretStr = Field(max_length=MAX_PASSWORD)
    new_password: SecretStr = Field(max_length=MAX_PASSWORD)


class Account(BaseModel):
    id: int
    email: str
    name: str


async def refuse_if_weak(password: SecretStr, user_inputs: list[str]) -> None:
    """A 422 with what's wrong with a new password and zxcvbn's advice. The problems are codes,
    for the app to translate: too_short, contains_user_input, blocklisted, weak, pwned..."""
    result = await passwords.evaluate(password.get_secret_value(), user_inputs=user_inputs)
    if not result.ok:
        detail = {"password": list(result.problems), "suggestions": list(result.suggestions)}
        raise HTTPException(422, detail)


@app.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(form: SignUp) -> Account:
    email = form.email.casefold()
    await refuse_if_weak(form.password, [email, form.name])
    if email in users:
        raise HTTPException(status.HTTP_409_CONFLICT, "There's an account with that address")
    hashed = await passwords.hash(form.password.get_secret_value())
    user = User(len(users) + 1, email, form.name, hashed)
    users[email] = user
    return Account(id=user.id, email=user.email, name=user.name)


async def authenticate(request: Request, email: str, password: SecretStr) -> User:
    """The user whose password it is; a 401 for a wrong one, an unknown address or a locked
    account, alike."""
    user = users.get(email.casefold())
    user_id, stored = (user.id, user.password) if user else (None, None)
    secret = password.get_secret_value()
    if user is None or not await passwords.verify(user_id, secret, stored, request=request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong e-mail or password")
    if passwords.needs_rehash(user.password):
        user.password = await passwords.hash(secret)
    return user


@app.post("/login")
async def login(request: Request, form: Login) -> Account:
    user = await authenticate(request, form.email, form.password)
    return Account(id=user.id, email=user.email, name=user.name)


@app.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(request: Request, form: PasswordChange) -> None:
    user = await authenticate(request, form.email, form.current_password)
    if form.new_password.get_secret_value() == form.current_password.get_secret_value():
        detail = {"password": ["unchanged"], "suggestions": []}
        raise HTTPException(422, detail)
    await refuse_if_weak(form.new_password, [user.email, user.name])
    user.password = await passwords.hash(form.new_password.get_secret_value())
    # With sessions, end the others now (UserSessions.end_all): they knew the old password.


if __name__ == "__main__":
    uvicorn.run(app)
