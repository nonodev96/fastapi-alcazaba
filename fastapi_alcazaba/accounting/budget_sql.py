"""The limits of `FastAPIProviderBudget` in a table of your database, for an admin panel to edit:
pip install "fastapi-alcazaba[sqlalchemy]".

    provider_budgets = provider_budgets_table(metadata)  # your migrations create it
    limits = SQLBudgetLimits(sessions, provider_budgets, "openai", plan=plan_of)

    openai = FastAPIProviderBudget(
        "openai", subject=current_user_id, limits=limits, total=limits.total
    )

    # In the admin panel:
    await limits.save(BudgetLimits(quota=5_000_000), plan="pro")
    await limits.save(BudgetLimits(quota=0, enabled=False), user="u-123")  # cut off
    openai.invalidate("u-123")  # this process at once; the others within settings_ttl

Each row holds the whole `BudgetLimits` of one scope: a user's, a plan's, every user's
(`default`), or the whole app's (`total`). A user gets the most specific one: theirs, else their
plan's, else the default, else the budget's `default`. A row's empty column means no limit, not
"as the plan's": a user's row says all of their limits.
"""

import time
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

from starlette.concurrency import run_in_threadpool

from fastapi_alcazaba.accounting.budget import BudgetLimits

try:
    from sqlalchemy import (
        BigInteger,
        Boolean,
        Column,
        Float,
        Integer,
        MetaData,
        String,
        Table,
        delete,
        insert,
        select,
    )
    from sqlalchemy.orm import Session
except ImportError as exc:  # pragma: no cover  # the extra isn't installed
    raise ImportError(
        'SQLBudgetLimits needs SQLAlchemy: pip install "fastapi-alcazaba[sqlalchemy]"'
    ) from exc
try:
    from sqlalchemy.ext.asyncio import AsyncSession
except ImportError:  # pragma: no cover  # greenlet isn't installed: no async sessions, then
    AsyncSession = None  # type: ignore[assignment,misc]

#: The scopes of a row: a user's, a plan's, every user's, the whole app's.
SCOPES = ("user", "plan", "default", "total")
_LIMITS = ("quota", "period", "rate", "rate_window", "concurrency", "max_per_call", "enabled")


def provider_budgets_table(metadata: MetaData, name: str = "provider_budgets") -> Table:
    """The table of the limits, in your metadata (your migrations create it): one row per
    provider, scope (`user`, `plan`, `default`, `total`) and name (the user's ID, the plan's
    name, "" for the other two), with the fields of `BudgetLimits` and when it was changed."""
    return Table(
        name,
        metadata,
        Column("provider", String(64), primary_key=True),
        Column("scope", String(8), primary_key=True),
        Column("name", String(200), primary_key=True),
        Column("quota", BigInteger, nullable=True),
        Column("period", String(8), nullable=False, default="month"),
        Column("rate", Integer, nullable=True),
        Column("rate_window", Integer, nullable=False, default=60),
        Column("concurrency", Integer, nullable=True),
        Column("max_per_call", BigInteger, nullable=True),
        Column("enabled", Boolean, nullable=False, default=True),
        Column("updated", Float, nullable=False),
    )


class SQLBudgetLimits:
    """Reads (and writes, for an admin panel) a provider's limits in `provider_budgets_table`.
    Called with a user's ID it gives their limits, as `FastAPIProviderBudget(limits=...)` wants;
    `total` gives the app's.

    :param sessions: makes the sessions: a `sessionmaker` or an `async_sessionmaker`.
    :param table: the table `provider_budgets_table` made.
    :param provider: the provider whose rows these are.
    :param plan: a function (plain or async) of a user's ID that gives their plan's name, None
        without one; without it, no plan rows are read.
    """

    def __init__(
        self,
        sessions: Callable[[], Any],
        table: Table,
        provider: str,
        *,
        plan: Callable[[str], str | None | Awaitable[str | None]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.table = table
        self.provider = provider
        self.plan = plan

    async def __call__(self, user: str) -> BudgetLimits | None:
        """`user`'s limits: their row's, else their plan's, else the default's; None without
        any (the budget's `default`)."""
        plan = None
        if self.plan is not None:
            found = self.plan(user)
            plan = await found if isawaitable(found) else found
        wanted = [("user", user), ("default", "")]
        if plan is not None:
            wanted.insert(1, ("plan", plan))
        rows = await self._rows(wanted)
        for scope, name in wanted:
            if (scope, name) in rows:
                return rows[scope, name]
        return None

    async def total(self) -> BudgetLimits | None:
        """The whole app's limits; None without a `total` row."""
        return (await self._rows([("total", "")])).get(("total", ""))

    async def save(
        self,
        limits: BudgetLimits,
        *,
        user: str | None = None,
        plan: str | None = None,
        scope: str | None = None,
    ) -> None:
        """Write the limits of a `user`, of a `plan`, or of `scope="default"` or `"total"`
        (one of them), replacing what was there."""
        found_scope, name = _scope(user, plan, scope)
        values = {field: getattr(limits, field) for field in _LIMITS}
        table = self.table
        where = (
            (table.c.provider == self.provider)
            & (table.c.scope == found_scope)
            & (table.c.name == name)
        )

        def work(session: Session) -> None:
            session.execute(delete(table).where(where))
            session.execute(
                insert(table).values(
                    provider=self.provider,
                    scope=found_scope,
                    name=name,
                    updated=time.time(),
                    **values,
                )
            )

        await self._run(work, commit=True)

    async def delete(
        self,
        *,
        user: str | None = None,
        plan: str | None = None,
        scope: str | None = None,
    ) -> bool:
        """Remove a row (as `save` names it): the user falls back to their plan's, and so on.
        Whether there was one."""
        found_scope, name = _scope(user, plan, scope)
        table = self.table
        statement = delete(table).where(
            (table.c.provider == self.provider)
            & (table.c.scope == found_scope)
            & (table.c.name == name)
        )
        result = await self._run(lambda session: session.execute(statement), commit=True)
        return bool(result.rowcount)

    async def _rows(self, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], BudgetLimits]:
        table = self.table
        names = {name for _, name in wanted}
        statement = select(table).where(
            (table.c.provider == self.provider)
            & table.c.scope.in_([scope for scope, _ in wanted])
            & table.c.name.in_(names)
        )
        rows = await self._run(lambda session: session.execute(statement).mappings().all())
        found = {}
        for row in rows:
            if (row["scope"], row["name"]) in wanted:
                found[row["scope"], row["name"]] = BudgetLimits(
                    **{field: row[field] for field in _LIMITS}
                )
        return found

    async def _run(self, work: Callable[[Session], Any], *, commit: bool = False) -> Any:
        """`work` in a session of `sessions`: an AsyncSession's run_sync, or a Session in a
        thread."""
        session = self.sessions()
        if AsyncSession is not None and isinstance(session, AsyncSession):
            async with session:
                result = await session.run_sync(work)
                if commit:
                    await session.commit()
                return result

        def blocking() -> Any:
            with session:
                result = work(session)
                if commit:
                    session.commit()
                return result

        return await run_in_threadpool(blocking)


def _scope(user: str | None, plan: str | None, scope: str | None) -> tuple[str, str]:
    given = [value is not None for value in (user, plan, scope)]
    if given.count(True) != 1:
        raise ValueError("give one of user, plan and scope")
    if user is not None:
        return "user", user
    if plan is not None:
        return "plan", plan
    if scope not in ("default", "total"):
        raise ValueError('scope is "default" or "total"')
    return scope, ""
