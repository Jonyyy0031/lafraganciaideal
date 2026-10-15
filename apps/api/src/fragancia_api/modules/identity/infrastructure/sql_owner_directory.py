from sqlalchemy import select

from fragancia_api.modules.identity.application.queries.owner_directory import OwnerContact
from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.modules.identity.infrastructure.tables import users
from fragancia_api.shared.infrastructure.database import Database


class SqlOwnerDirectory:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def active_owners(self) -> list[OwnerContact]:
        statement = (
            select(users.c.name, users.c.email)
            .where(users.c.role == Role.OWNER.value, users.c.is_active.is_(True))
            .order_by(users.c.name)
        )
        async with self._database.reader() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [OwnerContact(name=row["name"], email=row["email"]) for row in rows]
