"""An activity feed by cursor, newest first: each `next_cursor` gives the events right after the
last one seen, however many were added since.

    uvicorn examples.pagination.cursor:app

`PaginationCursor` hides the sort key of the last event, `(created_at, id)`, in a signed cursor:
clients can't make one up or change it. The database fetches one row more than the page, which
tells whether there's a next one. Set CURSOR_SECRET (secrets.token_urlsafe(32)), the same in
every process, in the environment or a .env file.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, status
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination import Position
from fastapi_alcazaba.pagination.cursor import CursorPage, CursorParams, PaginationCursor
from fastapi_alcazaba.response_cache import cache

START = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    cursor_secret: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class Event(BaseModel):
    id: int
    created_at: datetime
    text: str


class NewEvent(BaseModel):
    text: str = Field(min_length=1, max_length=200)


def sort_key(event: Event) -> tuple[datetime, int]:
    """Unique: two events can share a time, never an id."""
    return event.created_at, event.id


class Database:
    """Stands in for the database: 30 events, a minute apart."""

    def __init__(self) -> None:
        self.events = [
            Event(id=n, created_at=START + timedelta(minutes=n), text=f"Event {n}")
            for n in range(1, 31)
        ]

    def newest(self, before: Position | None, fetch: int) -> list[Event]:
        """SELECT * FROM events WHERE (created_at, id) < :before
        ORDER BY created_at DESC, id DESC LIMIT :fetch"""
        found = sorted(self.events, key=sort_key, reverse=True)
        if isinstance(before, tuple):  # the key of the last event of the page before
            found = [event for event in found if sort_key(event) < before]
        return found[:fetch]

    def add(self, event: NewEvent) -> Event:
        saved = Event(id=len(self.events) + 1, created_at=datetime.now(UTC), text=event.text)
        self.events.append(saved)
        return saved


db = Database()
# Signed: clients can't make cursors up. Every process must have the same secret, or a cursor
# made by one is refused by the others.
cursors = PaginationCursor(secret=settings.cursor_secret, default_limit=5, max_limit=20)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the pages.
    FastAPICache.init(InMemoryBackend(), prefix="feed", vary_headers=())  # public data
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/events")
@cache(expire=60)
async def events(params: Annotated[CursorParams, Depends(cursors)]) -> CursorPage[Event]:
    """One entry per cursor: the first page is renewed every minute, the others never change."""
    rows = db.newest(params.after, params.fetch)
    return cursors.paginate(rows, params, key=sort_key, descending=True, queried=True)


@app.post("/events", status_code=status.HTTP_201_CREATED)
async def add_event(event: NewEvent) -> Event:
    """A new event goes on top: the next pages of every client stay as they were."""
    return db.add(event)


if __name__ == "__main__":
    uvicorn.run("cursor:app", reload=True)
