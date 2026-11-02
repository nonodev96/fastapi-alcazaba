"""A site without passwords: people sign up and log in with a passkey, see the devices they're
logged in on, and log them out.

    uvicorn examples.passkeys.main:app

Open http://localhost:8000 in a browser with passkeys (Chrome, Safari, Edge, Firefox): its page
signs up with a username and a passkey (Touch ID, Windows Hello, a phone, a security key), then
logs in with it, without typing anything. `/sessions` lists the devices of the user, each of
which `DELETE /sessions/{ref}` logs out, and `/sessions/end-all` all of them but this one.
Deleting the account needs a passkey of the last five minutes.

Set CACHE_SIGNING_KEY (`secrets.token_urlsafe(32)`), the same in every process; WEBAUTHN_RP_ID
and WEBAUTHN_ORIGIN for another site than localhost.
"""

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPICache, FastAPIWebAuthn
from fastapi_alcazaba.auth import Passkey, WebAuthnRefused, credential_id
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import (
    CSRFMiddleware,
    ServerSessionMiddleware,
    UserSessions,
    clear_session,
    rotate_session,
)


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    webauthn_rp_id: str = "localhost"
    webauthn_origin: str = "http://localhost:8000"
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class User(BaseModel):
    id: str
    username: str
    handle: str  # the WebAuthn user handle: random, the same for all their passkeys
    passkeys: list[dict[str, Any]] = []  # Passkey.to_dict(): public keys, nothing secret


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.owners: dict[str, str] = {}  # credential id -> user id

    def by_name(self, username: str) -> User | None:
        return next((user for user in self.users.values() if user.username == username), None)


class SignUp(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9_]{3,30}$")


db = Database()
webauthn = FastAPIWebAuthn(
    rp_id=settings.webauthn_rp_id,
    rp_name="Example",
    origins=[settings.webauthn_origin],
)
user_sessions = UserSessions()


def current_user(request: Request) -> User:
    user = db.users.get(request.session.get("user_id", ""))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    return user


def current_user_id(request: Request) -> str | None:
    return request.session.get("user_id")


CurrentUser = Annotated[User, Depends(current_user)]


def log_in(request: Request, user: User) -> dict[str, str]:
    """The session is the user's from now on, and its passkey a recent second factor."""
    request.session.clear()
    request.session["user_id"] = user.id
    rotate_session(request)  # no session fixation
    webauthn.mark(request, user.id)  # a verified passkey: both factors at once
    return {"logged_in": user.username}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production: the challenges and the sessions, shared by every process.
    FastAPICache.init(InMemoryBackend(), prefix="passkeys", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(ServerSessionMiddleware, users=user_sessions, max_age=8 * 3600)
app.add_middleware(CSRFMiddleware)  # session cookies: refuse what other sites make browsers send


@app.post("/signup/options")
async def sign_up_options(request: Request, form: SignUp) -> dict[str, Any]:
    """The options for the browser to make a passkey for a new user, kept pending until it does."""
    if db.by_name(form.username) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is taken")
    user_id = uuid.uuid4().hex
    options = await webauthn.registration_options(user_id, form.username)
    request.session["signing_up"] = {
        "id": user_id,
        "username": form.username,
        "handle": options["user"]["id"],
    }
    return options


@app.post("/signup")
async def sign_up(request: Request, credential: dict[str, Any]) -> dict[str, str]:
    """The passkey the browser made: the user exists from now on, and is logged in."""
    pending = request.session.get("signing_up")
    if pending is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ask for the options first")
    try:
        passkey = await webauthn.verify_registration(pending["id"], credential, request=request)
    except WebAuthnRefused:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That passkey wasn't accepted") from None
    if passkey.id in db.owners:
        raise HTTPException(status.HTTP_409_CONFLICT, "That passkey is someone else's")
    user = User(**pending, passkeys=[passkey.to_dict()])
    db.users[user.id] = user
    db.owners[passkey.id] = user.id
    return log_in(request, user)


def find_passkey(passkey_id: str) -> tuple[User, Passkey]:
    """The passkey the browser answered with, and its user."""
    owner = db.users.get(db.owners.get(passkey_id, ""))
    for stored in owner.passkeys if owner is not None else []:
        if stored["id"] == passkey_id:
            return owner, Passkey.from_dict(stored)  # type: ignore[return-value]
    raise WebAuthnRefused("unknown", "no such passkey")


@app.post("/login/options")
async def login_options() -> dict[str, Any]:
    """The options for any passkey of this site: the browser asks which one."""
    return await webauthn.authentication_options()


@app.post("/login")
async def login(request: Request, credential: dict[str, Any]) -> dict[str, str]:
    """A passkey's answer: its user is logged in."""
    try:
        owner, passkey = find_passkey(credential_id(credential))
        updated = await webauthn.verify_authentication(
            credential, passkey, user_id=owner.id, request=request
        )
    except WebAuthnRefused:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That passkey wasn't accepted") from None
    owner.passkeys = [updated.to_dict() if p["id"] == updated.id else p for p in owner.passkeys]
    return log_in(request, owner)


@app.get("/me")
async def me(user: CurrentUser) -> dict[str, Any]:
    return {"username": user.username, "passkeys": len(user.passkeys)}


@app.get("/sessions")
async def sessions(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
    """The devices the user is logged in on, the newest first."""
    found = await user_sessions.list(user.id, request=request)
    return [
        {
            "ref": session.ref,
            "since": session.created.isoformat(),
            "last_seen": session.last_seen.isoformat(),
            "device": session.user_agent,
            "this_one": session.current,
        }
        for session in found
    ]


@app.delete("/sessions/{ref}", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(request: Request, ref: str, user: CurrentUser) -> None:
    """Log one of the user's devices out."""
    if not await user_sessions.end(user.id, ref, request=request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such session")


@app.post("/sessions/end-all")
async def end_all_sessions(request: Request, user: CurrentUser) -> dict[str, int]:
    """Log every other device out: a lost phone, a shared computer."""
    ended = await user_sessions.end_all(user.id, request=request, keep_current=True)
    return {"ended": ended}


@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> None:
    clear_session(request)


@app.delete(
    "/account",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(webauthn.require_recent(current_user_id, max_age=300))],
)
async def delete_account(request: Request, user: CurrentUser) -> None:
    """Needs a passkey of the last five minutes: a session left open isn't enough."""
    await user_sessions.end_all(user.id, request=request)
    for passkey in user.passkeys:
        db.owners.pop(passkey["id"], None)
    del db.users[user.id]
    clear_session(request)


@app.get("/", response_class=HTMLResponse)
async def page() -> str:
    return PAGE


PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Passkeys</title>
<h1>Passkeys</h1>
<p><input id="username" placeholder="username"> <button id="signup">Sign up</button>
<p><button id="login">Log in with a passkey</button>
<pre id="out"></pre>
<script>
const out = document.getElementById("out");
async function post(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  const answer = await response.json();
  if (!response.ok) throw new Error(answer.detail);
  return answer;
}
document.getElementById("signup").onclick = async () => {
  try {
    const username = document.getElementById("username").value;
    const options = await post("/signup/options", {username});
    const credential = await navigator.credentials.create(
      {publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options)});
    out.textContent = JSON.stringify(await post("/signup", credential.toJSON()));
  } catch (error) { out.textContent = error; }
};
document.getElementById("login").onclick = async () => {
  try {
    const options = await post("/login/options", {});
    const credential = await navigator.credentials.get(
      {publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options)});
    out.textContent = JSON.stringify(await post("/login", credential.toJSON()));
  } catch (error) { out.textContent = error; }
};
</script>
"""


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
