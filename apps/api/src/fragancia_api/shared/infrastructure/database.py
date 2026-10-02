"""Async SQLAlchemy engine, sessions and transactions.

The session of the active unit of work lives in a ContextVar, so repositories reach it through
`Database.session` without receiving it as a parameter (the Python counterpart of web-rh's
AsyncLocalStorage). Writes must happen inside `SqlTransactionRunner.run(...)`.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fragancia_api.shared.kernel import Ok, Result

_active_session: ContextVar[AsyncSession | None] = ContextVar("active_session", default=None)


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @property
    def session(self) -> AsyncSession:
        """The session of the active unit of work. Raises outside of one: a write without a
        transaction is a programming error."""
        session = _active_session.get()
        if session is None:
            raise RuntimeError(
                "No active transaction: run writes inside TransactionRunner.run(...)"
            )
        return session

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[AsyncSession]:
        """Open a session and make it the active one. The caller commits or rolls back."""
        async with self._sessions() as session:
            token = _active_session.set(session)
            try:
                yield session
            finally:
                _active_session.reset(token)

    @asynccontextmanager
    async def reader(self) -> AsyncIterator[AsyncSession]:
        """A short-lived session for queries (the read side of CQRS)."""
        async with self._sessions() as session:
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()


class SqlTransactionRunner:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        if _active_session.get() is not None:  # nested: join the outer transaction
            return await work()
        async with self._database.unit_of_work() as session:
            try:
                result = await work()
            except BaseException:
                await session.rollback()
                raise
            if isinstance(result, Ok):
                await session.commit()
            else:
                await session.rollback()
            return result
