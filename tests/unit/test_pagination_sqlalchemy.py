"""The SQLAlchemy queries of the pagination strategies, run against SQLite: each strategy walks a
table page by page, both ways where it can, and gets what paginating the whole list gets."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import Select, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from fastapi_alcazaba.pagination.cursor import PaginationCursor
from fastapi_alcazaba.pagination.keyset import PaginationKeyset
from fastapi_alcazaba.pagination.offset import PaginationOffset
from fastapi_alcazaba.pagination.page_number import PaginationPageNumber
from fastapi_alcazaba.pagination.ranges import PaginationRange
from fastapi_alcazaba.pagination.sqlalchemy import count_query, page_query, sort_key


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    price: Mapped[int]  # cents: two products may cost the same
    kind: Mapped[str] = mapped_column(String(10))


PRICES = [390, 420, 420, 590, 650, 650, 720, 780, 990, 990, 1150]
BY = (Product.price, Product.id)


@pytest.fixture(name="session")
def fixture_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            Product(id=i, name=f"Oil {i}", price=price, kind="oil" if i % 2 else "olive")
            for i, price in enumerate(PRICES, start=1)
        )
        session.commit()
        yield session
    engine.dispose()  # closes its connections


def rows(session: Session, statement: Select[Any]) -> list[Product]:
    return list(session.scalars(statement).all())


def ids(page: Any) -> list[int]:
    return [product.id for product in page.items]


CHEAPEST = sorted(range(1, 12), key=lambda i: (PRICES[i - 1], i))


class TestByPosition:
    def test_page_numbers(self, session: Session) -> None:
        pages = PaginationPageNumber()
        params = pages.params(page=2, page_size=4)
        statement = select(Product).where(Product.kind == "oil")
        total = session.scalar(count_query(statement.order_by(Product.id)))
        assert total == 6
        page = pages.paginate(
            rows(session, page_query(statement, params, by=BY)), params, total=total
        )
        assert (ids(page), page.pages) == ([9, 11], 2)

    def test_offsets_with_and_without_the_total(self, session: Session) -> None:
        offsets = PaginationOffset()
        params = offsets.params(offset=8, limit=4)
        found = rows(session, page_query(select(Product), params, by=BY))
        assert [p.id for p in found] == CHEAPEST[8:]  # 3 rows: no extra one at the end
        page = offsets.paginate(found, params, queried=True)
        assert (ids(page), page.has_more, page.total) == (CHEAPEST[8:], False, None)
        params = offsets.params(offset=0, limit=4)
        found = rows(session, page_query(select(Product), params, by=BY))
        assert len(found) == 5  # one more: there's a next page
        counted = offsets.paginate(
            found, params, total=session.scalar(count_query(select(Product)))
        )
        assert (ids(counted), counted.has_more, counted.total) == (CHEAPEST[:4], True, 11)

    def test_ranges(self, session: Session) -> None:
        ranges = PaginationRange()
        params = ranges.params(start=2, end=4)
        found = rows(session, page_query(select(Product), params, by=BY))
        page = ranges.paginate(found, params, total=11)
        assert ids(page) == CHEAPEST[2:5]

    def test_the_statement_s_own_order(self, session: Session) -> None:
        params = PaginationOffset().params(limit=3)
        statement = page_query(select(Product).order_by(Product.id.desc()), params)
        assert [p.id for p in rows(session, statement)] == [11, 10, 9, 8]

    def test_newest_first(self, session: Session) -> None:
        params = PaginationOffset().params(limit=2)
        statement = page_query(select(Product), params, by=(Product.id,), descending=True)
        assert [p.id for p in rows(session, statement)] == [11, 10, 9]


class TestByCursor:
    def walk(self, session: Session, *, descending: bool = False) -> list[list[int]]:
        cursors, key, seen = PaginationCursor("s" * 32), sort_key(*BY), []
        params = cursors.params(limit=4)
        while True:
            found = rows(session, page_query(select(Product), params, by=BY, descending=descending))
            page = cursors.paginate(found, params, key=key, descending=descending, queried=True)
            seen.append(ids(page))
            if page.next_cursor is None:
                break
            params = cursors.params(cursor=page.next_cursor, limit=4)
        back = page
        while back.prev_cursor is not None:
            params = cursors.params(cursor=back.prev_cursor, limit=4)
            found = rows(session, page_query(select(Product), params, by=BY, descending=descending))
            back = cursors.paginate(found, params, key=key, descending=descending, queried=True)
            seen.append(ids(back))
        return seen

    def test_there_and_back(self, session: Session) -> None:
        """Ties on the price are broken by the id: nothing skipped, nothing seen twice."""
        assert self.walk(session) == [
            CHEAPEST[:4],
            CHEAPEST[4:8],
            CHEAPEST[8:],
            CHEAPEST[4:8],
            CHEAPEST[:4],
        ]

    def test_dearest_first(self, session: Session) -> None:
        dearest = CHEAPEST[::-1]
        assert self.walk(session, descending=True) == [
            dearest[:4],
            dearest[4:8],
            dearest[8:],
            dearest[4:8],
            dearest[:4],
        ]

    def test_a_cursor_of_another_sort_key(self) -> None:
        cursors = PaginationCursor("s" * 32)
        params = cursors.params(cursor=cursors.encode(7))
        with pytest.raises(HTTPException) as refused:
            page_query(select(Product), params, by=BY)
        assert (refused.value.status_code, refused.value.detail) == (400, "Invalid cursor")

    def test_the_sort_key_is_needed(self) -> None:
        with pytest.raises(ValueError, match="sort key"):
            page_query(select(Product), PaginationCursor("s" * 32).params())


class TestByKeyset:
    def test_by_id(self, session: Session) -> None:
        keyset = PaginationKeyset(sort=int)
        params = keyset.params(after="8", limit=2)
        found = rows(session, page_query(select(Product), params, by=(Product.id,)))
        page = keyset.paginate(found, params, key=sort_key(Product.id), queried=True)
        assert (ids(page), page.after, page.has_more) == ([9, 10], "10", True)

    def test_by_price_and_id(self, session: Session) -> None:
        keyset = PaginationKeyset(sort=(int, int))
        params = keyset.params(after="[650, 5]", limit=3)
        found = rows(session, page_query(select(Product), params, by=BY))
        page = keyset.paginate(found, params, key=sort_key(*BY), queried=True)
        assert ids(page) == [6, 7, 8]  # 650 again, but after id 5

    def test_newest_first(self, session: Session) -> None:
        keyset = PaginationKeyset(sort=int)
        params = keyset.params(after="4", limit=5)
        statement = page_query(select(Product), params, by=(Product.id,), descending=True)
        found = rows(session, statement)
        page = keyset.paginate(
            found, params, key=sort_key(Product.id), descending=True, queried=True
        )
        assert (ids(page), page.has_more) == ([3, 2, 1], False)


class TestSortKey:
    def test_values_by_name(self) -> None:
        product = Product(id=3, name="Oil", price=420, kind="oil")
        assert sort_key(*BY)(product) == (420, 3)
        assert sort_key(Product.id)(product) == 3

    def test_columns_are_needed_and_named(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            sort_key()
        with pytest.raises(TypeError, match="label it"):
            sort_key(Product.price + 1)


def test_the_count_ignores_order_limit_and_offset(session: Session) -> None:
    statement = select(Product).order_by(Product.price).limit(2).offset(3)
    assert session.scalar(count_query(statement)) == 11
