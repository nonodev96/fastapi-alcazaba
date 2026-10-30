"""An activity log streamed as NDJSON, one event per line, without pages and without loading it.

    uvicorn examples.pagination.streaming:app

- `/events`: every event (or every event of a kind), read from the database 100 at a time by
  id, each batch sent as soon as it's read. A client that stops reading stops the queries.
- `/events/latest`: the 25 newest, from a synchronous cursor that the stream reads in the thread
  pool and closes at the 25th.

Streams aren't cached: each request reads the database again.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from fastapi_alcazaba.pagination.streaming import PaginationStreaming


class Kind(StrEnum):
    LOGIN = "login"
    UPLOAD = "upload"
    LOGOUT = "logout"


class Event(BaseModel):
    id: int
    kind: Kind
    at: datetime


class Database:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        kinds, start = list(Kind), datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
        self.events = [
            Event(id=i, kind=kinds[i % len(kinds)], at=start + timedelta(minutes=i))
            for i in range(1, 251)
        ]
        self.queries = 0
        self.open_cursors = 0

    async def events_after(
        self,
        after: int | None,
        limit: int,
        kind: Kind | None,
    ) -> list[Event]:
        """What a database does: WHERE id > :after AND kind = :kind ORDER BY id LIMIT :limit."""
        self.queries += 1
        found = [
            event
            for event in self.events
            if (after is None or event.id > after) and (kind is None or event.kind == kind)
        ]
        return found[:limit]

    def newest(self) -> Iterator[Event]:
        """A synchronous driver's cursor, newest first: the rows come as they're read."""
        self.queries += 1
        self.open_cursors += 1
        try:
            yield from reversed(self.events)
        finally:
            self.open_cursors -= 1


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.db = Database()
    yield


app = FastAPI(lifespan=lifespan)


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_db)]

# 100 lines a chunk, and 100 rows a query.
stream = PaginationStreaming(batch_size=100)
latest = PaginationStreaming(batch_size=10, max_items=25)


@app.get("/events")
async def events(
    db: DatabaseDep,
    kind: Kind | None = None,
) -> StreamingResponse:
    """Every event, by id: a query per batch, past the last id of the batch before."""

    async def fetch(
        after: int | None,
        limit: int,
    ) -> list[Event]:
        return await db.events_after(after, limit, kind)

    return stream.response(stream.batches(fetch, key=lambda event: event.id))


@app.get("/events/latest")
async def latest_events(db: DatabaseDep) -> StreamingResponse:
    """The 25 newest events: the stream stops reading the cursor there, and closes it."""
    return latest.response(db.newest())


if __name__ == "__main__":
    uvicorn.run("streaming:app", reload=True)
