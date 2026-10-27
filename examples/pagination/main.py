"""A product catalogue paginated and cached by Alcazaba, with no other library.

    uvicorn examples.pagination.main:app

Three ways to cache paginated lists:

- `/products`: `@cache` on the endpoint, one entry per page. Each response is served straight from
  the cache. Best for big lists, where most clients only read the first pages.
- `/categories/{category}/products`: the whole list cached once with `get_or_set`, and every page
  sliced from it. One entry, whatever the page and size. Best for short lists.
- `/products/newest`: by cursor, newest first, one entry per cursor. Products added meanwhile
  don't shift the pages, which suits infinite scroll and clients that sync everything.

They're tagged "products", so a write drops every page at once without knowing their keys.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import (
    Cache,
    FastAPICache,
    FastAPIPagination,
    FastAPIPaginationCursor,
    get_cache,
)
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.pagination import CursorPage, CursorParams, Page, PageParams
from fastapi_alcazaba.response_cache import cache


class Category(StrEnum):
    # An enum, not any string: clients can't create cache entries without end.
    FRUIT = "fruit"
    VEGETABLES = "vegetables"
    OIL = "oil"


class NewProduct(BaseModel):
    name: str
    category: Category
    price: float


class Product(NewProduct):
    id: int


class Catalog:
    """Stands in for the database (a session per request in a real app)."""

    def __init__(self) -> None:
        categories = list(Category)
        self.products = [
            Product(id=i, name=f"Product {i}", category=categories[i % 3], price=1 + i / 10)
            for i in range(1, 61)
        ]
        self.queries = 0

    def find(self, category: Category | None = None) -> list[Product]:
        self.queries += 1
        return [p for p in self.products if category is None or p.category == category]

    def page(self, params: PageParams, category: Category | None = None) -> Page[Product]:
        """What a database does: count, and fetch only the page (OFFSET/LIMIT)."""
        found = self.find(category)
        rows = found[params.offset : params.offset + params.limit]
        return pages.paginate(rows, params, total=len(found))

    def newest(self, params: CursorParams) -> CursorPage[Product]:
        """What a database does: WHERE id < :after ORDER BY id DESC LIMIT :limit."""
        self.queries += 1
        newest = sorted(self.products, key=lambda p: p.id, reverse=True)
        if isinstance(params.after, int):  # the id of the last product of the page before
            newest = [p for p in newest if p.id < params.after]
        rows = newest[: params.limit]
        return cursors.paginate(rows, params, key=lambda p: p.id, queried=True)

    def add(self, product: NewProduct) -> Product:
        saved = Product(id=len(self.products) + 1, **product.model_dump())
        self.products.append(saved)
        return saved


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the entries.
    FastAPICache.init(InMemoryBackend(), prefix="catalog", vary_headers=())  # public data
    application.state.catalog = Catalog()
    yield


app = FastAPI(lifespan=lifespan)


def get_catalog(request: Request) -> Catalog:
    catalog: Catalog = request.app.state.catalog
    return catalog


CatalogDep = Annotated[Catalog, Depends(get_catalog)]


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    cursor_secret: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()
pages = FastAPIPagination(default_size=20, max_size=100)
# Signed: clients can't make cursors up. Every process must have the same secret, or a cursor
# made by one is refused by the others: set CURSOR_SECRET (secrets.token_urlsafe(32)).
cursors = FastAPIPaginationCursor(secret=settings.cursor_secret, default_size=20)


@app.get("/products")
@cache(expire=300, tags=["products"], exclude_params=["catalog"])
async def products(
    params: Annotated[PageParams, Depends(pages)],
    catalog: CatalogDep,
    category: Category | None = None,
) -> Page[Product]:
    """One entry per page, size and category: the page parameters are arguments, part of the
    key. The session is left out of it, since it changes from one request to the next."""
    return catalog.page(params, category)


@app.get("/categories/{category}/products")
async def category_products(
    category: Category,
    params: Annotated[PageParams, Depends(pages)],
    catalog: CatalogDep,
    app_cache: Annotated[Cache, Depends(get_cache)],
) -> Page[Product]:
    """One entry for the whole category: every page is sliced from it."""

    async def load() -> list[Product]:  # annotated: the entry comes back as products
        return catalog.find(category)

    found = await app_cache.get_or_set(f"category:{category}", load, expire=300, tags=["products"])
    return pages.paginate(found, params)


@app.get("/products/newest")
@cache(expire=300, tags=["products"], exclude_params=["catalog"])
async def newest_products(
    params: Annotated[CursorParams, Depends(cursors)],
    catalog: CatalogDep,
) -> CursorPage[Product]:
    """By cursor, newest first: each next_cursor gives the products right after the last one seen,
    however many were added since."""
    return catalog.newest(params)


@app.post("/products", status_code=status.HTTP_201_CREATED)
async def add_product(product: NewProduct, catalog: CatalogDep) -> Product:
    """The new product may land on any page: drop them all."""
    saved = catalog.add(product)
    await FastAPICache.invalidate_tags("products")
    return saved


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
