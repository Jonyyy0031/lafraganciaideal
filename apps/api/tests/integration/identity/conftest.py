from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.identity.domain.session import Session
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.sql_session_repository import (
    SqlSessionRepository,
)
from fragancia_api.modules.identity.infrastructure.sql_user_repository import SqlUserRepository
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Ok, Result

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
IDLE = timedelta(minutes=120)


@pytest.fixture(autouse=True)
async def clean_identity(container: Container) -> None:
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE identity.sessions, identity.users, identity.login_throttle")
        )


async def in_transaction[T](container: Container, work: Callable[[], Awaitable[T]]) -> T:
    """Run `work` in one committed unit of work (the repositories need an active one)."""

    async def unit() -> Result[T, DomainError]:
        return Ok(await work())

    result = await SqlTransactionRunner(container.database).run(unit)
    assert isinstance(result, Ok)
    value: T = result.value
    return value


def make_user(email: str = "owner@example.test", *, role: Role = Role.OWNER) -> User:
    return User.create(Email(email), DisplayName("Owner Test"), role, "hash-1", now=NOW)


def make_session(
    user_id: UUID,
    token_hash: str,
    *,
    opened: datetime = NOW,
    max_age: timedelta = timedelta(hours=12),
) -> Session:
    return Session.open(
        user_id, token_hash, now=opened, max_age=max_age, user_agent="tests", ip="203.0.113.7"
    )


async def store_user(container: Container, user: User) -> None:
    result = await in_transaction(
        container, lambda: SqlUserRepository(container.database).add(user)
    )
    assert isinstance(result, Ok)


async def store_session(container: Container, session: Session) -> None:
    await in_transaction(container, lambda: SqlSessionRepository(container.database).add(session))
