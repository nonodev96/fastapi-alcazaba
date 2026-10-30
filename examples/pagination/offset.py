"""An access log paginated by offset, without the total: a table too big to count at every
request, and that grows while it's read.

    uvicorn examples.pagination.offset:app

`/logs?offset=0&limit=20` reads one row more than the page, which says whether there's a next one
(`has_more`), instead of counting the whole log: the newest lines first, all of them or those of
one level. Each page is cached for a few seconds, under its own key.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.offset import OffsetPage, OffsetParams, PaginationOffset
from fastapi_alcazaba.response_cache import cache


class Level(StrEnum):
    # An enum, not any string: clients can't create cache entries without end.
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Line(BaseModel):
    id: int
    level: Level
    message: str


def level_of(number: int) -> Level:
    if number % 50 == 0:
        return Level.ERROR
    return Level.WARNING if number % 10 == 0 else Level.INFO


class Database:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        self.lines = [
            Line(id=i, level=level_of(i), message=f"GET /products/{i % 97}") for i in range(1, 1001)
        ]
        self.queries = 0

    def page(
        self,
        params: OffsetParams,
        level: Level | None = None,
    ) -> OffsetPage[Line]:
        """What a database does: WHERE level = :level ORDER BY id DESC LIMIT :fetch OFFSET
        :offset, one row more than the page and no count."""
        self.queries += 1
        newest = [line for line in reversed(self.lines) if level is None or line.level == level]
        rows = newest[params.offset : params.offset + params.fetch]
        return offsets.paginate(rows, params, queried=True)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    FastAPICache.init(InMemoryBackend(), prefix="logs", vary_headers=())
    application.state.db = Database()
    yield


app = FastAPI(lifespan=lifespan)


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_db)]

offsets = PaginationOffset(default_limit=20, max_limit=100)


@app.get("/logs")
@cache(expire=10, exclude_params=["db"])
async def logs(
    params: Annotated[OffsetParams, Depends(offsets)],
    db: DatabaseDep,
    level: Level | None = None,
) -> OffsetPage[Line]:
    """The newest lines first, a page at a time: `has_more` says whether to ask for the next."""
    return db.page(params, level)


if __name__ == "__main__":
    uvicorn.run("offset:app", reload=True)
