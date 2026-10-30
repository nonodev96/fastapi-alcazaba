"""A cooperative's news feed, by time: newest first for readers, oldest first for syncing.

    uvicorn examples.pagination.time_based:app

- `/posts`: the feed, newest first. `next_timestamp` is the time of the page's last post, and
  `?timestamp=<next_timestamp>` gives the posts before it.
- `/posts/since?timestamp=...`: the posts after a time, oldest first, for a client that syncs:
  it keeps the time of the last post it got, and asks for the ones after it.

Posts can share a time (the three price lists of 09:00 came in one import), and a page never
splits them: when they don't all fit, the page ends before them and the next one starts with
them all.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.time_based import (
    PaginationTimeBased,
    TimeBasedPage,
    TimeBasedParams,
)
from fastapi_alcazaba.response_cache import cache


class NewPost(BaseModel):
    title: str


class Post(NewPost):
    id: int
    posted_at: datetime


def posted_at(post: Post) -> datetime:
    return post.posted_at


class Database:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        news = [
            ("08:15", "The harvest starts in Jaén"),
            ("08:40", "Frost warning for the groves"),
            ("09:00", "Price list: Picual"),
            ("09:00", "Price list: Hojiblanca"),
            ("09:00", "Price list: Arbequina"),
            ("09:30", "The mill opens on Saturday"),
            ("10:00", "Tasting at the cooperative"),
            ("10:00", "Recipe: salmorejo"),
            ("10:20", "The bottling line is back"),
        ]
        self.posts = [
            Post(id=i, title=title, posted_at=datetime.fromisoformat(f"2026-10-29T{time}Z"))
            for i, (time, title) in enumerate(news, start=1)
        ]
        self.queries = 0

    def older(self, params: TimeBasedParams) -> list[Post]:
        """What a database does, through an index on posted_at:
        WHERE posted_at < :timestamp ORDER BY posted_at DESC LIMIT :fetch."""
        self.queries += 1
        timestamp = params.timestamp
        found = [p for p in self.posts if timestamp is None or p.posted_at < timestamp]
        return sorted(found, key=lambda p: (p.posted_at, p.id), reverse=True)[: params.fetch]

    def newer(self, params: TimeBasedParams) -> list[Post]:
        """WHERE posted_at > :timestamp ORDER BY posted_at LIMIT :fetch."""
        self.queries += 1
        timestamp = params.timestamp
        found = [p for p in self.posts if timestamp is None or p.posted_at > timestamp]
        return sorted(found, key=lambda p: (p.posted_at, p.id))[: params.fetch]

    def add(self, post: NewPost) -> Post:
        saved = Post(id=len(self.posts) + 1, title=post.title, posted_at=datetime.now(UTC))
        self.posts.append(saved)
        return saved


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the entries.
    FastAPICache.init(InMemoryBackend(), prefix="news", vary_headers=())  # public data
    application.state.db = Database()
    yield


app = FastAPI(lifespan=lifespan)


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_db)]

feed = PaginationTimeBased("older", default_limit=3, max_limit=50)
sync = PaginationTimeBased("newer", default_limit=3, max_limit=50)


def page(
    pages: PaginationTimeBased,
    rows: list[Post],
    params: TimeBasedParams,
) -> TimeBasedPage[Post]:
    try:
        return pages.paginate(rows, params, time=posted_at, queried=True)
    except ValueError:
        # More posts share one time than the page holds: a bigger page takes them.
        raise HTTPException(
            400,
            f"More than {params.limit} posts share a time: ask for more",
        ) from None


@app.get("/posts")
@cache(expire=60, tags=["posts"], exclude_params=["db"])
async def posts(
    params: Annotated[TimeBasedParams, Depends(feed)],
    db: DatabaseDep,
) -> TimeBasedPage[Post]:
    """Newest first: the posts before `timestamp`."""
    return page(feed, db.older(params), params)


@app.get("/posts/since")
@cache(expire=60, tags=["posts"], exclude_params=["db"])
async def posts_since(
    params: Annotated[TimeBasedParams, Depends(sync)],
    db: DatabaseDep,
) -> TimeBasedPage[Post]:
    """Oldest first: the posts after `timestamp`."""
    return page(sync, db.newer(params), params)


@app.post("/posts", status_code=status.HTTP_201_CREATED)
async def add_post(post: NewPost, db: DatabaseDep) -> Post:
    """The newest post: the first page of the feed and the last of every sync change."""
    saved = db.add(post)
    await FastAPICache.invalidate_tags("posts")
    return saved


if __name__ == "__main__":
    uvicorn.run("time_based:app", reload=True)
