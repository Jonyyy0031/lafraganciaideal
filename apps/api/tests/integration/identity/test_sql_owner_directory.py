"""Plan 004 on PostgreSQL: the `OwnerDirectory` facade over `SqlOwnerDirectory` (role and active
filters, ordering). Rows are the test's own and are removed afterwards."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.identity import OwnerContact, OwnerDirectory
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.sql_owner_directory import SqlOwnerDirectory
from fragancia_api.modules.identity.infrastructure.sql_user_repository import SqlUserRepository
from tests.integration.identity.conftest import NOW, in_transaction, store_user

pytestmark = pytest.mark.integration

DOMAIN = "@owner-directory.test"


@pytest.fixture(autouse=True)
async def remove_own_rows(container: Container) -> AsyncIterator[None]:
    yield
    pattern = {"pattern": f"%{DOMAIN}"}
    async with container.database.engine.begin() as connection:
        # Prove the WHERE first: this file's own domain, at most the 4 rows a test adds.
        mine = (
            await connection.execute(
                text("SELECT count(*) FROM identity.users WHERE email LIKE :pattern"), pattern
            )
        ).scalar_one()
        assert mine <= 4
        await connection.execute(
            text("DELETE FROM identity.users WHERE email LIKE :pattern"), pattern
        )


async def add(
    container: Container, name: str, role: Role = Role.OWNER, *, active: bool = True
) -> User:
    user = User.create(
        Email(f"{name.lower().replace(' ', '-')}{DOMAIN}"), DisplayName(name), role, "h", now=NOW
    )
    await store_user(container, user)
    if not active:
        user.deactivate()
        repository = SqlUserRepository(container.database)
        await in_transaction(container, lambda: repository.save(user))
    return user


def directory(container: Container) -> OwnerDirectory:
    return OwnerDirectory(SqlOwnerDirectory(container.database))


async def test_no_users_means_no_owners(container: Container) -> None:
    assert await directory(container).active_owners() == []


async def test_only_active_owners_are_returned(container: Container) -> None:
    await add(container, "Ana")
    await add(container, "Gone", active=False)
    await add(container, "Staff", Role.STAFF)
    await add(container, "Staff Off", Role.STAFF, active=False)

    assert await directory(container).active_owners() == [
        OwnerContact(name="Ana", email=f"ana{DOMAIN}")
    ]


async def test_owners_come_back_ordered_by_name(container: Container) -> None:
    await add(container, "Zoe")
    await add(container, "Ana")
    await add(container, "Mar")

    owners = await directory(container).active_owners()

    assert [owner.name for owner in owners] == ["Ana", "Mar", "Zoe"]


async def test_a_deactivated_owner_drops_out_and_a_reactivated_one_returns(
    container: Container,
) -> None:
    user = await add(container, "Ana")
    repository = SqlUserRepository(container.database)

    user.deactivate()
    await in_transaction(container, lambda: repository.save(user))
    assert await directory(container).active_owners() == []

    user.reactivate()
    await in_transaction(container, lambda: repository.save(user))
    assert [o.name for o in await directory(container).active_owners()] == ["Ana"]


async def test_the_container_registers_the_facade_over_the_sql_directory(
    container: Container,
) -> None:
    await add(container, "Ana")

    owners = await container.services.get(OwnerDirectory).active_owners()

    assert owners == [OwnerContact(name="Ana", email=f"ana{DOMAIN}")]
