from typing import Any
from uuid import UUID

from psycopg import errors as pg_errors
from sqlalchemy import RowMapping, insert, select, update
from sqlalchemy.exc import IntegrityError

from fragancia_api.modules.identity.domain.errors import EmailTaken
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.tables import USER_EMAIL_UNIQUE, users
from fragancia_api.shared.infrastructure.database import Database
from fragancia_api.shared.kernel import Err, Ok, Result


def _to_row(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email.value,
        "name": user.name.value,
        "role": user.role.value,
        "password_hash": user.password_hash,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "password_changed_at": user.password_changed_at,
    }


def _to_user(row: RowMapping) -> User:
    return User(
        id=row["id"],
        email=Email(row["email"]),
        name=DisplayName(row["name"]),
        role=Role(row["role"]),
        password_hash=row["password_hash"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        password_changed_at=row["password_changed_at"],
    )


class SqlUserRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, user_id: UUID) -> User | None:
        return await self._one(select(users).where(users.c.id == user_id))

    async def get_for_update(self, user_id: UUID) -> User | None:
        return await self._one(select(users).where(users.c.id == user_id).with_for_update())

    async def get_by_email(self, email: Email) -> User | None:
        return await self._one(select(users).where(users.c.email == email.value))

    async def add(self, user: User) -> Result[None, EmailTaken]:
        session = self._database.session
        try:
            # Savepoint: a unique violation must not abort the surrounding transaction.
            async with session.begin_nested():
                await session.execute(insert(users).values(_to_row(user)))
        except IntegrityError as error:
            if _violates(error, USER_EMAIL_UNIQUE):
                return Err(EmailTaken())
            raise
        return Ok(None)

    async def save(self, user: User) -> None:
        await self._database.session.execute(
            update(users)
            .where(users.c.id == user.id)
            .values(
                password_hash=user.password_hash,
                password_changed_at=user.password_changed_at,
                is_active=user.is_active,
            )
        )

    async def _one(self, statement: Any) -> User | None:
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_user(row) if row is not None else None


def _violates(error: IntegrityError, constraint: str) -> bool:
    cause = error.orig
    return isinstance(cause, pg_errors.UniqueViolation) and cause.diag.constraint_name == constraint
