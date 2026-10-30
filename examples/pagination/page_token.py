"""A library's books, paginated by page token: opaque, signed, bound to the query and short-lived.

    uvicorn examples.pagination.page_token:app

`/books?genre=poetry&page_size=3` answers the first page and its `next_page_token`; the next page
is `/books?genre=poetry&page_size=3&page_token=<next_page_token>`. A client can't make a token
up, use it with other filters (`genre=novel`), or keep it for more than ten minutes: any of these
is a 400. Each page is cached for a minute, under a key of its own.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination.page_token import PaginationToken, TokenPage, TokenParams
from fastapi_alcazaba.response_cache import cache


class Genre(StrEnum):
    # An enum, not any string: clients can't create cache entries without end.
    POETRY = "poetry"
    NOVEL = "novel"
    ESSAY = "essay"


class Book(BaseModel):
    id: int
    title: str
    genre: Genre


class Database:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        genres = list(Genre)
        self.books = [
            Book(id=i, title=f"Book {i}", genre=genres[i % len(genres)]) for i in range(1, 31)
        ]
        self.queries = 0

    def page(
        self,
        params: TokenParams,
        genre: Genre | None,
    ) -> list[Book]:
        """What a database does: WHERE genre = :genre ORDER BY id LIMIT :fetch OFFSET :offset."""
        self.queries += 1
        found = [book for book in self.books if genre is None or book.genre == genre]
        return found[params.offset : params.offset + params.fetch]


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the entries.
    FastAPICache.init(InMemoryBackend(), prefix="library", vary_headers=())  # public data
    application.state.db = Database()
    yield


app = FastAPI(lifespan=lifespan)


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DatabaseDep = Annotated[Database, Depends(get_db)]


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    page_token_secret: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()
# Signed: every process must have the same secret, or a token made by one is refused by the
# others: set PAGE_TOKEN_SECRET (secrets.token_urlsafe(32)). Ten minutes to read a list.
tokens = PaginationToken(
    settings.page_token_secret,
    ttl=600,
    default_page_size=10,
    max_page_size=50,
)


@app.get("/books")
@cache(expire=60, exclude_params=["db"])
async def books(
    params: Annotated[TokenParams, Depends(tokens)],
    db: DatabaseDep,
    genre: Genre | None = None,
) -> TokenPage[Book]:
    """By page token: each next_page_token gives the books after the page, for the same genre.
    The cache keeps every page of every query apart, and is kept for far less than the ten
    minutes a token lasts."""
    return tokens.paginate(db.page(params, genre), params, queried=True)


if __name__ == "__main__":
    uvicorn.run("page_token:app", reload=True)
