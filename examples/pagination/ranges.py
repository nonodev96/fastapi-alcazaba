"""A contact list for a data grid, paginated by range: the grid asks for the rows it shows by their
indexes, and reads which ones it got, and how many there are, from `Content-Range`.

    uvicorn examples.pagination.ranges:app

`/contacts?start=0&end=24` answers the first 25 contacts with `Content-Range: items 0-24/319`. A
range that starts past the last contact is a 416, with `Content-Range: items */319`. Each range
is cached under its own key, and a new contact drops them all.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.ranges import PaginationRange, RangePage, RangeParams
from fastapi_alcazaba.response_cache import cache


class NewContact(BaseModel):
    name: str
    email: str


class Contact(NewContact):
    id: int


class Database:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        self.contacts = [
            Contact(id=i, name=f"Contact {i:03}", email=f"contact{i}@example.com")
            for i in range(1, 320)
        ]
        self.queries = 0

    def page(self, params: RangeParams) -> RangePage[Contact]:
        """What a database does: count, and fetch only the range (ORDER BY name LIMIT :limit
        OFFSET :offset)."""
        self.queries += 1
        by_name = sorted(self.contacts, key=lambda contact: contact.name)
        rows = by_name[params.offset : params.offset + params.limit]
        return ranges.paginate(rows, params, total=len(by_name))

    def add(self, contact: NewContact) -> Contact:
        saved = Contact(id=len(self.contacts) + 1, **contact.model_dump())
        self.contacts.append(saved)
        return saved


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    FastAPICache.init(InMemoryBackend(), prefix="contacts", vary_headers=())
    application.state.db = Database()
    yield


app = FastAPI(lifespan=lifespan)


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_db)]

ranges = PaginationRange(default_items=25, max_items=100)


@app.get("/contacts")
@cache(expire=300, tags=["contacts"], exclude_params=["db"])
async def contacts(
    params: Annotated[RangeParams, Depends(ranges)],
    db: DatabaseDep,
) -> RangePage[Contact]:
    """A range of contacts, by name: the cache gives a hit its Content-Range too."""
    return db.page(params)


@app.post("/contacts", status_code=status.HTTP_201_CREATED)
async def add_contact(contact: NewContact, db: DatabaseDep) -> Contact:
    """The total changes, and the new contact may land in any range: drop them all."""
    saved = db.add(contact)
    await FastAPICache.invalidate_tags("contacts")
    return saved


if __name__ == "__main__":
    uvicorn.run("ranges:app", reload=True)
