"""API keys that Unkey keeps: people sign up and get a key, their scripts send it, and Unkey
checks it on every request, with its permissions, its credits and its rate limit.

    uvicorn examples.auth_strategies.unkey_keys:app

Each key reads documents (`documents.read`), has 100 credits a month, of which writing a
document takes 5 and needs `documents.write`, which signing up doesn't give, and 10 requests a
minute. Its owner deletes it with `DELETE /keys/{key_id}`, and Unkey refuses it from then on.

Create an API in Unkey's dashboard, and two root keys: one that may only verify its keys, one
that may create and delete them. Set UNKEY_VERIFY_KEY, UNKEY_ADMIN_KEY and UNKEY_API_ID, in the
environment or a .env file.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPIAuth
from fastapi_alcazaba.auth.strategies import UnkeyStrategy
from fastapi_alcazaba.auth.strategies.unkey import (
    CreditRefill,
    KeyRatelimit,
    UnkeyClient,
    UnkeyKey,
    UnkeyKeys,
)


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    unkey_verify_key: SecretStr = SecretStr("set UNKEY_VERIFY_KEY")
    unkey_admin_key: SecretStr = SecretStr("set UNKEY_ADMIN_KEY")
    unkey_api_id: str = "set_UNKEY_API_ID"


settings = Settings()


class User(BaseModel):
    id: str
    username: str


class SignUp(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9_]{3,30}$")


class NewKey(BaseModel):
    key: str  # shown once: Unkey keeps only its hash
    key_id: str


class Document(BaseModel):
    title: str = Field(max_length=200)
    owner: str = ""


class Database:
    """Stands in for the database: the users, and the ids of their keys."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.keys_of: dict[str, set[str]] = {}
        self.documents = [Document(title="Style guide", owner="unkey")]


db = Database()
# A root key for each use, with only the permissions it needs.
verifier = UnkeyClient(settings.unkey_verify_key)
admin = UnkeyClient(settings.unkey_admin_key)
keys = UnkeyKeys(admin, api_id=settings.unkey_api_id, prefix="docs")


def load_user(key: UnkeyKey) -> User | None:
    """The owner of a valid key: the user its external ID names."""
    return db.users.get(key.external_id) if key.external_id is not None else None


auth = FastAPIAuth(
    UnkeyStrategy(verifier, load_user, permissions="documents.read"),
    # Writing needs another permission and costs more: a strategy of its own, the same client.
    UnkeyStrategy(verifier, load_user, name="unkey-write", permissions="documents.write", cost=5),
)
Reader = Annotated[User, Depends(auth.authenticate("unkey"))]
Writer = Annotated[User, Depends(auth.authenticate("unkey-write"))]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await verifier.aclose()
    await admin.aclose()


app = FastAPI(lifespan=lifespan)


@app.post("/signup", status_code=status.HTTP_201_CREATED)
async def sign_up(form: SignUp) -> NewKey:
    """A new user and its first key, which reads documents: shown once."""
    if any(user.username == form.username for user in db.users.values()):
        raise HTTPException(409, "That username is taken")
    user = User(id=f"user_{uuid.uuid4().hex[:12]}", username=form.username)
    created = await keys.create(
        external_id=user.id,
        name=f"{user.username}'s first key",
        credits_left=100,
        refill=CreditRefill(100, day=1),
        ratelimits=[KeyRatelimit("requests", 10, timedelta(minutes=1))],
        permissions=["documents.read"],
    )
    db.users[user.id] = user
    db.keys_of[user.id] = {created.key_id}
    return NewKey(key=created.key.get_secret_value(), key_id=created.key_id)


@app.get("/documents")
async def list_documents(user: Reader) -> list[Document]:
    """One credit of the key, and one request of its rate limit."""
    del user  # authenticated: that's all it needs
    return db.documents


@app.post("/documents", status_code=status.HTTP_201_CREATED)
async def write_document(document: Document, user: Writer) -> Document:
    """For keys with documents.write: five credits."""
    written = Document(title=document.title, owner=user.username)
    db.documents.append(written)
    return written


@app.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(key_id: str, user: Reader) -> None:
    """Deletes one of the user's keys, this one too: Unkey refuses it from then on."""
    if key_id not in db.keys_of.get(user.id, set()):
        raise HTTPException(404, "No such key")  # someone else's keys don't exist for them
    await keys.delete(key_id)
    db.keys_of[user.id].discard(key_id)


if __name__ == "__main__":
    uvicorn.run("unkey_keys:app", reload=True)
