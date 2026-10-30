"""A shop's activity log, read in order after the sort values of the last event seen.

    uvicorn examples.pagination.search_after:app

`GET /events?limit=3` gives the first three events, and `search_after` the time and id of the
last one, as a JSON array: `["2026-10-29T09:58:30Z",3]`. `?search_after=<that array>` gives
the next three. Clients read those values, and may write their own: the events from 10:00 on are
`?search_after=["2026-10-29T10:00:00Z",0]`, since every id comes after 0.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.search_after import (
    PaginationSearchAfter,
    SearchAfterPage,
    SearchAfterParams,
)
from fastapi_alcazaba.response_cache import cache


class Event(BaseModel):
    id: int
    at: datetime
    action: str


def by_time(event: Event) -> tuple[datetime, int]:
    """The sort values: the time, and the id between events of the same time."""
    return event.at, event.id


class Database:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        log = [
            ("09:58:00", "login"),
            ("09:58:30", "cart.updated"),
            ("09:58:30", "cart.updated"),
            ("09:59:15", "order.created"),
            ("10:00:00", "payment.authorized"),
            ("10:00:00", "order.confirmed"),
            ("10:00:00", "email.sent"),
            ("10:01:40", "order.shipped"),
            ("10:03:20", "email.sent"),
            ("10:05:00", "logout"),
        ]
        self.events = [
            Event(id=i, at=datetime.fromisoformat(f"2026-10-29T{time}Z"), action=action)
            for i, (time, action) in enumerate(log, start=1)
        ]
        self.queries = 0

    def page(self, params: SearchAfterParams) -> list[Event]:
        """What a database does, through an index on (at, id):
        WHERE (at, id) > :search_after ORDER BY at, id LIMIT :fetch."""
        self.queries += 1
        after = params.search_after
        found = [e for e in self.events if after is None or by_time(e) > after]
        return sorted(found, key=by_time)[: params.fetch]


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the entries.
    FastAPICache.init(InMemoryBackend(), prefix="activity")
    application.state.db = Database()
    yield


app = FastAPI(lifespan=lifespan)


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_db)]

# The types of the sort values: a datetime (which must come with a time zone) and an int.
events_after = PaginationSearchAfter(sort=(datetime, int), default_limit=3, max_limit=100)


@app.get("/events")
@cache(expire=60, exclude_params=["db"])
async def events(
    params: Annotated[SearchAfterParams, Depends(events_after)],
    db: DatabaseDep,
) -> SearchAfterPage[Event]:
    """Oldest first. One entry per position and limit: the page parameters are part of the key,
    the session isn't."""
    return events_after.paginate(db.page(params), params, key=by_time, queried=True)


if __name__ == "__main__":
    uvicorn.run("search_after:app", reload=True)
