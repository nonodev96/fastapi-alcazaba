"""The queries of the pagination strategies, written for SQLAlchemy: a `select()` limited to the
page that the parameters ask for, the count of the whole list, and the sort key of a row.

    from fastapi_alcazaba.pagination.sqlalchemy import count_query, page_query, sort_key

    by = (Event.created_at, Event.id)

    @app.get("/events")
    async def events(params: Annotated[CursorParams, Depends(cursors)]) -> CursorPage[Event]:
        rows = (await session.scalars(page_query(select(Event), params, by=by))).all()
        return cursors.paginate(rows, params, key=sort_key(*by), queried=True)

`page_query` writes what's easy to get wrong by hand: the comparison of the whole sort key as a
row value (`(created_at, id) > (:a, :b)`, which PostgreSQL, MySQL 8, MariaDB and SQLite 3.15+
read through an index), the order that goes with it, backwards for a previous page, and the
extra row that tells whether there's another page. `pip install "fastapi-alcazaba[sqlalchemy]"`.
"""

from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from fastapi import HTTPException
from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from fastapi_alcazaba.pagination.cursor import CursorParams
from fastapi_alcazaba.pagination.keyset import KeysetParams
from fastapi_alcazaba.pagination.offset import OffsetParams
from fastapi_alcazaba.pagination.page_number import PageNumberParams
from fastapi_alcazaba.pagination.ranges import RangeParams
from fastapi_alcazaba.pagination.values import Position

SelectT = TypeVar("SelectT", bound=Select[Any])

#: A column of the sort key: a mapped attribute (`Event.id`) or a column expression.
Column = InstrumentedAttribute[Any] | ColumnElement[Any]


def page_query(
    statement: SelectT,
    params: OffsetParams | PageNumberParams | RangeParams | CursorParams | KeysetParams,
    *,
    by: Sequence[Column] = (),
    descending: bool = False,
) -> SelectT:
    """`statement` limited to the page of `params`.

    By position (`OffsetParams`, `PageNumberParams`, `RangeParams`): `OFFSET` and `LIMIT`, in the
    order `statement` already has, or the one `by` gives. `OffsetParams` fetch one row more,
    which `PaginationOffset.paginate(..., queried=True)` takes to tell whether there's a next
    page; with `total=` it leaves the extra row out.

    By key (`CursorParams`, `KeysetParams`): `by` is the sort key, unique as a whole (`(Event.
    created_at, Event.id)`), and the statement must have no `ORDER BY` of its own. The rows past
    `params.after`, in the order of `by` (`descending` for newest first), or for a previous page
    those before `params.before` in the reverse order, nearest first, as `paginate(...,
    queried=True)` expects them; one more than the limit in either case.
    """
    if isinstance(params, CursorParams | KeysetParams):
        if not by:
            raise ValueError("give the columns of the sort key: by=(Model.created_at, Model.id)")
        before = params.before if isinstance(params, CursorParams) else None
        backwards = before is not None
        ascending = descending == backwards  # the order the rows are read in
        statement = statement.order_by(*(c.asc() if ascending else c.desc() for c in by))
        position = before if backwards else params.after
        if position is not None:
            key = tuple_(*by) if len(by) > 1 else by[0]
            name = "cursor" if isinstance(params, CursorParams) else "after"
            values = _values(position, len(by), name)
            bound = tuple_(*values) if len(by) > 1 else values[0]
            statement = statement.where(key > bound if ascending else key < bound)
        return statement.limit(params.fetch)
    if by:
        statement = statement.order_by(*(c.desc() if descending else c.asc() for c in by))
    limit = params.fetch if isinstance(params, OffsetParams) else params.limit
    return statement.offset(params.offset).limit(limit)


def count_query(
    statement: Select[Any],
) -> Select[int]:
    """The count of the rows of `statement`, without its order, limit or offset: the `total` of
    `PaginationPageNumber`, `PaginationOffset` and `PaginationRange`."""
    inner = statement.order_by(None).limit(None).offset(None).subquery()
    return select(func.count()).select_from(inner)


def sort_key(
    *columns: Column,
) -> Callable[[Any], Position]:
    """The `key` of `paginate` for these columns: a row's values of them, read by their names
    (`sort_key(Event.created_at, Event.id)` gives `(event.created_at, event.id)`); one column's
    value alone."""
    if not columns:
        raise ValueError("give at least one column")
    names = [_name(column) for column in columns]
    if len(names) == 1:
        (name,) = names
        return lambda row: getattr(row, name)
    return lambda row: tuple(getattr(row, name) for name in names)


def _name(
    column: Column,
) -> str:
    name = getattr(column, "key", None)
    if not isinstance(name, str):
        raise TypeError(f"{column!r} has no name to read it from a row by: label it")
    return name


def _values(
    position: Position,
    count: int,
    name: str,
) -> tuple[Any, ...]:
    """The values of `position` for a key of `count` columns. A 400 when they don't match: a
    cursor of another endpoint, signed with the same secret."""
    values = position if isinstance(position, tuple) else (position,)
    if len(values) != count:
        raise HTTPException(400, f"Invalid {name}")
    return values
