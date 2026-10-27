"""An admin API whose sensitive actions leave an audit trail: who did what to which account,
whether it was allowed, and the request it came from.

    uvicorn examples.audit.main:app

- Changing a role and deleting an account are recorded when they end: `success`, `denied` (a 401
  without a token, a 403 for someone who isn't an admin) or `failure` (a 404 for an account that
  doesn't exist). Reading the trail is recorded too.
- Each event is a line of JSON on standard output, apart from the app's other logs, for the log
  pipeline to ship to storage that can't be rewritten. The events are signed and chained:
  `GET /audit/verify` says whether any was altered or removed.
- A sign-in records its outcome with `audit.log`, never the password, and the response carries
  the request ID that the event holds.

The accounts are a dict in memory here, a table in your database; the last events are kept in
memory too, for `/audit/events` and `/audit/verify`.
"""

import hmac
import logging
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import HTTPConnection

from fastapi_alcazaba import FastAPIAudit
from fastapi_alcazaba.audit import AuditEntry, LoggingAuditSink, MemoryAuditSink, verify_audit_trail
from fastapi_alcazaba.observability import RequestIDMiddleware


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Signs the events: AUDIT_SIGNING_KEY (secrets.token_urlsafe(32)), the same in every process,
    # kept from the people whose actions are recorded: with it, they could rewrite the trail.
    audit_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()

# The trail: one line of JSON per event on standard output, without the app's log format.
trail_log = logging.getLogger("fastapi_alcazaba.audit.events")
trail_log.addHandler(logging.StreamHandler(sys.stdout))
trail_log.setLevel(logging.INFO)
trail_log.propagate = False

recent = MemoryAuditSink(maxlen=1000)
audit = FastAPIAudit([LoggingAuditSink(trail_log), recent], signing_key=settings.audit_signing_key)

Role = Literal["admin", "support", "viewer"]


class Account(BaseModel):
    id: str
    email: str
    role: Role


class RoleChange(BaseModel):
    role: Role


class SignIn(BaseModel):
    email: str
    password: str


# Stands in for your users table, its password hashes and your access tokens.
PASSWORDS = {"ada@example.com": "correct horse", "sam@example.com": "battery staple"}
TOKENS = {"ada-token": "u1", "sam-token": "u2"}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.accounts = {
        "u1": Account(id="u1", email="ada@example.com", role="admin"),
        "u2": Account(id="u2", email="sam@example.com", role="support"),
        "u3": Account(id="u3", email="vic@example.com", role="viewer"),
    }
    yield


app = FastAPI(lifespan=lifespan)
# The ID of each request goes in its events and in the response: support finds the event a
# user's complaint is about.
app.add_middleware(RequestIDMiddleware)


def accounts(request: Request) -> dict[str, Account]:
    return request.app.state.accounts  # type: ignore[no-any-return]


Accounts = Annotated[dict[str, Account], Depends(accounts)]


async def current_account(connection: HTTPConnection) -> Account:
    """Stands in for your authentication: the account whose bearer token the request carries."""
    scheme, _, token = connection.headers.get("authorization", "").partition(" ")
    account_id = TOKENS.get(token) if scheme.lower() == "bearer" else None
    account = connection.app.state.accounts.get(account_id)
    if account is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return account  # type: ignore[no-any-return]


Me = Annotated[Account, Depends(current_account)]


def require_admin(entry: AuditEntry, me: Account) -> None:
    """Who acts goes in the event before the check: a refusal is recorded with its actor."""
    entry.actor = me.id
    if me.role != "admin":
        entry.reason = f"{me.role}, not admin"
        raise HTTPException(403, "Only admins can do this")


def find(all_accounts: dict[str, Account], account_id: str) -> Account:
    account = all_accounts.get(account_id)
    if account is None:
        raise HTTPException(404, "No such account")
    return account


# The audit dependency goes before the authentication (Me): it records what that one refuses,
# a 401, with the account from the path.
changing_role = audit.action("account.role.change", resource="account:{account_id}")
deleting = audit.action("account.delete", resource="account:{account_id}")
reading_trail = audit.action("audit.read")


@app.put("/accounts/{account_id}/role")
async def change_role(
    account_id: str,
    change: RoleChange,
    entry: Annotated[AuditEntry, Depends(changing_role)],
    me: Me,
    all_accounts: Accounts,
) -> Account:
    require_admin(entry, me)
    account = find(all_accounts, account_id)
    entry.details.update({"from": account.role, "to": change.role})
    account.role = change.role
    return account


@app.delete("/accounts/{account_id}", status_code=204)
async def delete_account(
    account_id: str,
    entry: Annotated[AuditEntry, Depends(deleting)],
    me: Me,
    all_accounts: Accounts,
) -> None:
    require_admin(entry, me)
    entry.details["email"] = find(all_accounts, account_id).email
    del all_accounts[account_id]


@app.post("/sessions")
async def sign_in(form: SignIn, request: Request, all_accounts: Accounts) -> dict[str, str]:
    account = next((a for a in all_accounts.values() if a.email == form.email), None)
    expected = PASSWORDS.get(form.email, "")
    # Compared in constant time, and the same answer whatever was wrong.
    if account is None or not hmac.compare_digest(form.password.encode(), expected.encode()):
        await audit.log(
            "session.create",
            outcome="denied",
            actor=account.id if account else None,
            reason="unknown e-mail" if account is None else "wrong password",
            request=request,
        )
        raise HTTPException(401, "Wrong e-mail or password")
    await audit.log("session.create", actor=account.id, request=request)
    token = next(token for token, owner in TOKENS.items() if owner == account.id)
    return {"access_token": token, "token_type": "bearer"}


@app.get("/audit/events")
async def audit_events(
    entry: Annotated[AuditEntry, Depends(reading_trail)],
    me: Me,
    limit: int = 20,
) -> list[dict[str, Any]]:
    require_admin(entry, me)
    return [event.to_dict() for event in list(recent.events)[-limit:]]


@app.get("/audit/verify")
async def audit_verify(
    entry: Annotated[AuditEntry, Depends(reading_trail)],
    me: Me,
) -> dict[str, Any]:
    require_admin(entry, me)
    events = list(recent.events)
    return {
        "events": len(events),
        "problems": verify_audit_trail(events, settings.audit_signing_key),
    }


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
