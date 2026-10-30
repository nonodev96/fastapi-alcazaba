"""A blog's archive by page number: numbered pages of posts, newest first, with the total and the
number of pages.

    uvicorn examples.pagination.page_number:app

`PaginationPageNumber` reads `page` and `page_size` from the query (a 422 for a page size over
20), the database counts the posts and fetches only the page, and the response links to the
first, previous, next and last pages. Each page is cached under its own key; publishing a post
drops them all, by tag.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, status
from pydantic import BaseModel, Field

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.page_number import (
    PageNumberPage,
    PageNumberParams,
    PaginationPageNumber,
)
from fastapi_alcazaba.response_cache import cache


class Post(BaseModel):
    id: int
    title: str


class NewPost(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class Database:
    """Stands in for the database: 45 posts, and how many queries it ran."""

    def __init__(self) -> None:
        self.posts = [Post(id=n, title=f"Post {n}") for n in range(1, 46)]
        self.queries = 0

    def count(self) -> int:
        """SELECT count(*) FROM posts"""
        self.queries += 1
        return len(self.posts)

    def newest(self, offset: int, limit: int) -> list[Post]:
        """SELECT * FROM posts ORDER BY id DESC LIMIT :limit OFFSET :offset"""
        self.queries += 1
        return sorted(self.posts, key=lambda post: post.id, reverse=True)[offset : offset + limit]

    def add(self, post: NewPost) -> Post:
        saved = Post(id=len(self.posts) + 1, title=post.title)
        self.posts.append(saved)
        return saved


db = Database()
pages = PaginationPageNumber(default_page_size=10, max_page_size=20)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the pages.
    FastAPICache.init(InMemoryBackend(), prefix="blog", vary_headers=())  # public data
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/posts")
@cache(expire=300, tags=["posts"])
async def posts(params: Annotated[PageNumberParams, Depends(pages)]) -> PageNumberPage[Post]:
    """Only the page, and the count: what a database does with OFFSET and LIMIT."""
    rows = db.newest(params.offset, params.limit)
    return pages.paginate(rows, params, total=db.count())


@app.post("/posts", status_code=status.HTTP_201_CREATED)
async def publish(post: NewPost) -> Post:
    """The new post goes first and shifts every page: drop them all."""
    saved = db.add(post)
    await FastAPICache.invalidate_tags("posts")
    return saved


if __name__ == "__main__":
    uvicorn.run("page_number:app", reload=True)
