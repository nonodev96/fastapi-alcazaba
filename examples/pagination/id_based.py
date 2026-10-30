"""A chat's messages paginated by id: clients ask for what came after the last message they have,
or page back through the history, newest first.

    uvicorn examples.pagination.id_based:app

`/messages?last_id=42&limit=20` gives the messages after 42, in order: a client that keeps the
last id it's seen gets only the new ones, however many arrive meanwhile. `/messages/newest`
pages back from the newest, `last_id` being the oldest message of the page before. Each page is
cached under its own key, and a new message drops them all.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.id_based import IdBasedPage, IdBasedParams, PaginationIdBased
from fastapi_alcazaba.response_cache import cache


class NewMessage(BaseModel):
    author: str
    text: str


class Message(NewMessage):
    id: int


class Database:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        authors = ("ana", "leo", "eva")
        self.messages = [
            Message(id=i, author=authors[i % 3], text=f"Message {i}") for i in range(1, 61)
        ]
        self.queries = 0

    def after(self, params: IdBasedParams) -> IdBasedPage[Message]:
        """What a database does: WHERE id > :last_id ORDER BY id LIMIT :fetch."""
        self.queries += 1
        rows = self.messages
        if isinstance(params.last_id, int):
            rows = [message for message in rows if message.id > params.last_id]
        return ids.paginate(rows[: params.fetch], params, key=lambda m: m.id, queried=True)

    def before(self, params: IdBasedParams) -> IdBasedPage[Message]:
        """What a database does: WHERE id < :last_id ORDER BY id DESC LIMIT :fetch."""
        self.queries += 1
        rows = self.messages[::-1]
        if isinstance(params.last_id, int):
            rows = [message for message in rows if message.id < params.last_id]
        return ids.paginate(
            rows[: params.fetch],
            params,
            key=lambda m: m.id,
            descending=True,
            queried=True,
        )

    def add(self, message: NewMessage) -> Message:
        saved = Message(id=len(self.messages) + 1, **message.model_dump())
        self.messages.append(saved)
        return saved


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    FastAPICache.init(InMemoryBackend(), prefix="chat", vary_headers=())
    application.state.db = Database()
    yield


app = FastAPI(lifespan=lifespan)


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_db)]

ids = PaginationIdBased(default_limit=20, max_limit=100)  # int ids: the query parameter's type


@app.get("/messages")
@cache(expire=300, tags=["messages"], exclude_params=["db"])
async def messages(
    params: Annotated[IdBasedParams, Depends(ids)],
    db: DatabaseDep,
) -> IdBasedPage[Message]:
    """The messages after `last_id`, oldest first: for clients that sync."""
    return db.after(params)


@app.get("/messages/newest")
@cache(expire=300, tags=["messages"], exclude_params=["db"])
async def newest_messages(
    params: Annotated[IdBasedParams, Depends(ids)],
    db: DatabaseDep,
) -> IdBasedPage[Message]:
    """The messages before `last_id`, newest first: for a history read backwards."""
    return db.before(params)


@app.post("/messages", status_code=status.HTTP_201_CREATED)
async def add_message(message: NewMessage, db: DatabaseDep) -> Message:
    """The last pages change: drop them all."""
    saved = db.add(message)
    await FastAPICache.invalidate_tags("messages")
    return saved


if __name__ == "__main__":
    uvicorn.run("id_based:app", reload=True)
