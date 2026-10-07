from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.identity.domain.user import Role
from tests.unit.identity.conftest import LINKS, Identity

INVITER = UUID(int=1)


async def test_users_lists_everyone_with_their_status_oldest_first(identity: Identity) -> None:
    first = identity.add_user("a@example.test", name="Ana Uno")
    identity.clock.current += timedelta(minutes=1)
    second = identity.add_user("b@example.test", role=Role.STAFF, active=False, name="Beto Dos")

    users = await identity.list_users.execute()

    assert [(u.id, u.email, u.name, u.role, u.is_active) for u in users] == [
        (first.id, "a@example.test", "Ana Uno", "owner", True),
        (second.id, "b@example.test", "Beto Dos", "staff", False),
    ]
    assert users[0].created_at < users[1].created_at


async def test_users_exposes_no_secrets(identity: Identity) -> None:
    identity.add_user()

    [user] = await identity.list_users.execute()

    assert set(user.model_dump()) == {"id", "email", "name", "role", "is_active", "created_at"}


async def test_pending_invitations_lists_newest_first(identity: Identity) -> None:
    await identity.invite_user.execute(INVITER, "old@example.test", "Vieja Uno")
    identity.clock.current += timedelta(minutes=1)
    await identity.invite_user.execute(INVITER, "new@example.test", "Nueva Dos")

    pending = await identity.list_pending_invitations.execute()

    assert [i.email for i in pending] == ["new@example.test", "old@example.test"]


async def test_pending_invitations_skips_revoked_accepted_and_expired(identity: Identity) -> None:
    await identity.invite_user.execute(INVITER, "kept@example.test", "Se Queda")
    for email in ("revoked@example.test", "accepted@example.test", "expired@example.test"):
        await identity.invite_user.execute(INVITER, email, "Otra Persona")
    by_email = {i.email.value: i for i in identity.invitations.by_id.values()}
    by_email["revoked@example.test"].revoke(identity.clock.now())
    by_email["accepted@example.test"].accept(identity.clock.now())
    by_email["expired@example.test"].expires_at = identity.clock.now()  # expiry is exclusive

    pending = await identity.list_pending_invitations.execute()

    assert [i.email for i in pending] == ["kept@example.test"]


async def test_an_invitation_stops_being_listed_at_its_expiry(identity: Identity) -> None:
    await identity.invite_user.execute(INVITER, "kept@example.test", "Se Queda")
    identity.clock.current += LINKS.invitation_ttl - timedelta(seconds=1)
    assert len(await identity.list_pending_invitations.execute()) == 1

    identity.clock.current += timedelta(seconds=1)

    assert await identity.list_pending_invitations.execute() == []
