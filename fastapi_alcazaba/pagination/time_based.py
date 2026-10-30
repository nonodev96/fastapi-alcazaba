"""Pagination by time: `?limit=20`, then `?timestamp=2026-10-29T10:00:00Z&limit=20`, the items
older than the timestamp (newest first, for feeds) or newer (oldest first, for syncing). A page
never splits the items that share a time, so that the next one, which starts past it, skips
none."""

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Generic, Literal

from fastapi import Request, Response
from pydantic import AwareDatetime, Field
from pydantic_core import to_jsonable_python

from fastapi_alcazaba.pagination.common import (
    REQUEST,
    LinkedPage,
    PageLinks,
    PaginationParams,
    Relation,
    T,
    check_limits,
    linked,
    query,
)

#: Which way the pages go from a timestamp: to older items, or to newer ones.
Direction = Literal["older", "newer"]


@dataclass(frozen=True, slots=True)
class TimeBasedParams(PaginationParams):
    """What `PaginationTimeBased` reads from the query: `limit` and the `timestamp` the page
    starts past, in UTC (none for the first page)."""

    limit: int = 50
    timestamp: datetime | None = None
    links: PageLinks | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit starts at 1")
        if self.timestamp is not None and self.timestamp.utcoffset() is None:
            raise ValueError("timestamp needs a time zone")

    @property
    def fetch(self) -> int:
        """Rows to query from the database: `limit` and one more, which tells whether there's a
        next page, and whether the page's last items share their time with the next ones."""
        return self.limit + 1


class TimeBasedPage(LinkedPage, Generic[T]):
    """A page of `items` by time; `next_timestamp` is None on the last one."""

    items: list[T]
    limit: int = Field(description="Items per page; more when the last ones share their time")
    next_timestamp: datetime | None = Field(
        description="Where the next page starts: the time of the last item; None at the end",
    )

    def relations(self) -> list[Relation]:
        stamps: list[tuple[str, str | None]] = [("first", None)]
        if self.next_timestamp is not None:
            # As the JSON has it: ISO 8601, "Z" for UTC.
            stamps.append(("next", to_jsonable_python(self.next_timestamp)))
        return [(rel, {"timestamp": stamp, "limit": self.limit}) for rel, stamp in stamps]


class PaginationTimeBased:
    """Pagination by time: `?limit=20`, then `?timestamp=<next_timestamp>&limit=20`.

        feed = PaginationTimeBased()  # older, newest first

        @app.get("/posts")
        @cache(expire=60)
        async def posts(params: Annotated[TimeBasedParams, Depends(feed)]) -> TimeBasedPage[Post]:
            rows = ...  # WHERE posted_at < params.timestamp ORDER BY posted_at DESC
            #             LIMIT params.fetch
            return feed.paginate(rows, params, time=lambda p: p.posted_at, queried=True)

    The timestamp is plain ISO 8601, with a time zone (a 422 without one): clients read it, and
    may as well send one of their own (the posts before yesterday). The next page starts past
    the time of a page's last item, so a page never splits the items that share a time: with the
    whole list, `paginate` takes the rest of them into the page; with queried rows, it leaves
    them all to the next one.

    :param direction: "older" for the items before the timestamp, newest first (feeds), or
        "newer" for those after it, oldest first (syncing).
    :param default_limit: items per page when the request doesn't say.
    :param max_limit: the most a request may ask for (a 422 beyond).
    """

    def __init__(
        self,
        direction: Direction = "older",
        *,
        default_limit: int = 50,
        max_limit: int = 100,
    ) -> None:
        check_limits(default_limit, max_limit, "limit")
        if direction not in ("older", "newer"):
            raise ValueError('direction is "older" or "newer"')
        self.direction = direction
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.__signature__ = inspect.Signature(
            [
                *REQUEST,
                query(
                    "timestamp",
                    AwareDatetime | None,
                    None,
                    description=(
                        "Where the page starts, with a time zone: the next_timestamp of the page"
                        " before"
                    ),
                ),
                query(
                    "limit",
                    int,
                    default_limit,
                    ge=1,
                    le=max_limit,
                    description="Items per page",
                ),
            ],
            return_annotation=TimeBasedParams,
        )

    async def __call__(
        self,
        *,
        request: Request,
        response: Response,
        timestamp: datetime | None = None,
        limit: int | None = None,
    ) -> TimeBasedParams:
        params = self.params(timestamp=timestamp, limit=limit)
        return replace(params, links=PageLinks.of(request, response))

    def params(
        self,
        *,
        timestamp: datetime | None = None,
        limit: int | None = None,
    ) -> TimeBasedParams:
        """The parameters of a page, as the dependency gives them (for tests and scripts), with
        the timestamp in UTC: one cache entry per instant, whatever the offset it came with.
        Raises ValueError if the timestamp has no time zone."""
        limit = self.default_limit if limit is None else limit
        if limit > self.max_limit:
            raise ValueError(f"limit can't be over {self.max_limit}")
        if timestamp is not None and timestamp.utcoffset() is not None:
            timestamp = timestamp.astimezone(UTC)
        return TimeBasedParams(limit=limit, timestamp=timestamp)

    def paginate(
        self,
        items: Sequence[T],
        params: TimeBasedParams,
        *,
        time: Callable[[T], datetime],
        queried: bool = False,
    ) -> TimeBasedPage[T]:
        """The page of `items` that `params` asks for, never splitting the items that share a
        time.

        :param time: an item's time, with a time zone.
        :param queried: `items` is what the database returned for this page: up to
            `params.fetch` rows with a time past `params.timestamp`, in order (`WHERE t <
            :timestamp ORDER BY t DESC` for "older", `WHERE t > :timestamp ORDER BY t` for
            "newer"). When the extra row shares its time with the last ones, those are left to
            the next page; when that leaves none, more than `limit` items share one time and a
            ValueError says to paginate by a unique key (`PaginationKeyset`). Otherwise `items`
            is the whole list, newest first ("older") or oldest first ("newer"), and the page
            takes the rest of the items that share its last time, past `limit` if need be.
        """
        if queried:
            rows = list(items)
            page, more = rows[: params.limit], len(rows) > params.limit
            if more:
                page = self._without_last_time(page, time(rows[params.limit]), time)
        else:
            start = self._start(items, params, time)
            stop = start + params.limit
            while stop < len(items) and time(items[stop]) == time(items[stop - 1]):
                stop += 1
            page, more = list(items[start:stop]), stop < len(items)
        next_timestamp = time(page[-1]) if more and page else None
        if next_timestamp is not None and next_timestamp.utcoffset() is None:
            raise ValueError("time must give datetimes with a time zone")
        return linked(
            TimeBasedPage(items=page, limit=params.limit, next_timestamp=next_timestamp),
            params.links,
        )

    def _start(
        self,
        items: Sequence[T],
        params: TimeBasedParams,
        time: Callable[[T], datetime],
    ) -> int:
        """Where the page starts in the whole list: at the first item past the timestamp."""
        timestamp = params.timestamp
        if timestamp is None:
            return 0
        older = self.direction == "older"
        return next(
            (
                index
                for index, item in enumerate(items)
                if (time(item) < timestamp if older else time(item) > timestamp)
            ),
            len(items),
        )

    @staticmethod
    def _without_last_time(
        page: list[T],
        following: datetime,
        time: Callable[[T], datetime],
    ) -> list[T]:
        """`page` without its last items when they share their time with the row that follows,
        which the next page would skip."""
        stop = len(page)
        while stop > 0 and time(page[stop - 1]) == following:
            stop -= 1
        if stop == 0:
            raise ValueError(
                f"more than {len(page)} items share the time {following.isoformat()}: a page"
                " can't hold them, and splitting them would skip some. Paginate by a unique key"
                " (PaginationKeyset, with the time and the id) instead"
            )
        return page[:stop]
