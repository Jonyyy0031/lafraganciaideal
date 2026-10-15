"""Plan 004, application: the `OwnerDirectory` facade over the in-memory user store."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from fragancia_api.modules.identity import OwnerContact, OwnerDirectory
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.in_memory import InMemoryUsers

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


async def add(
    users: InMemoryUsers, email: str, name: str, role: Role = Role.OWNER, *, active: bool = True
) -> None:
    user = User.create(Email(email), DisplayName(name), role, "hash", now=NOW)
    if not active:
        user.deactivate()
    await users.save(user)


async def test_no_users_means_no_owners() -> None:
    assert await OwnerDirectory(InMemoryUsers()).active_owners() == []


async def test_only_active_owners_are_listed() -> None:
    users = InMemoryUsers()
    await add(users, "ana@example.test", "Ana")
    await add(users, "gone@example.test", "Gone", active=False)
    await add(users, "staff@example.test", "Staff", Role.STAFF)
    await add(users, "staff-off@example.test", "Staff Off", Role.STAFF, active=False)

    assert await OwnerDirectory(users).active_owners() == [
        OwnerContact(name="Ana", email="ana@example.test")
    ]


async def test_owners_are_ordered_by_name() -> None:
    users = InMemoryUsers()
    await add(users, "zoe@example.test", "Zoe")
    await add(users, "ana@example.test", "Ana")
    await add(users, "mar@example.test", "Mar")

    owners = await OwnerDirectory(users).active_owners()

    assert [o.name for o in owners] == ["Ana", "Mar", "Zoe"]


async def test_a_reactivated_owner_is_listed_again() -> None:
    users = InMemoryUsers()
    await add(users, "ana@example.test", "Ana", active=False)
    assert await OwnerDirectory(users).active_owners() == []

    [user] = users.by_id.values()
    user.reactivate()

    assert [o.email for o in await OwnerDirectory(users).active_owners()] == ["ana@example.test"]


def test_a_contact_is_frozen() -> None:
    contact = OwnerContact(name="Ana", email="ana@example.test")

    with pytest.raises(FrozenInstanceError):
        contact.name = "Other"  # type: ignore[misc]
