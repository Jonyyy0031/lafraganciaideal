from uuid import UUID

from fragancia_api.modules.identity.domain.password_reset import PasswordReset
from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.identity.conftest import LINKS, Identity

STAFF = "staff@example.test"


async def test_deactivating_a_user_makes_them_inactive(identity: Identity) -> None:
    owner = identity.add_user()
    staff = identity.add_user(STAFF, role=Role.STAFF)

    result = await identity.deactivate_user.execute(owner.id, staff.id)

    assert isinstance(result, Ok)
    assert staff.is_active is False
    assert owner.is_active is True


async def test_deactivating_closes_every_session_of_that_user_and_only_theirs(
    identity: Identity,
) -> None:
    owner = identity.add_user()
    staff = identity.add_user(STAFF, role=Role.STAFF)
    staff_one = await identity.sign_in(STAFF)
    staff_two = await identity.sign_in(STAFF)
    owner_login = await identity.sign_in()

    await identity.deactivate_user.execute(owner.id, staff.id)

    sessions = [s for s in identity.sessions.by_id.values() if s.user_id == staff.id]
    assert len(sessions) == 2
    assert all(s.revoked_at == identity.clock.now() for s in sessions)
    assert staff_one.token != staff_two.token
    assert await identity.resolver.resolve(owner_login.token) is not None


async def test_deactivating_cancels_the_open_resets_of_that_user(identity: Identity) -> None:
    owner = identity.add_user()
    staff = identity.add_user(STAFF, role=Role.STAFF)
    reset = PasswordReset.request(staff.id, now=identity.clock.now(), ttl=LINKS.reset_ttl)
    await identity.resets.add(reset)

    await identity.deactivate_user.execute(owner.id, staff.id)

    assert reset.cancelled_at == identity.clock.now()
    assert not reset.is_pending(identity.clock.now())


async def test_a_deactivated_user_cannot_sign_in(identity: Identity) -> None:
    owner = identity.add_user()
    staff = identity.add_user(STAFF, role=Role.STAFF)
    await identity.deactivate_user.execute(owner.id, staff.id)

    result = await identity.log_in.execute(
        STAFF, "correct horse battery", ip="203.0.113.7", user_agent="t"
    )

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVALID_CREDENTIALS"


async def test_nobody_can_deactivate_themselves(identity: Identity) -> None:
    owner = identity.add_user()

    result = await identity.deactivate_user.execute(owner.id, owner.id)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_CANNOT_DEACTIVATE_SELF"
    assert owner.is_active is True


async def test_an_owner_can_deactivate_another_owner(identity: Identity) -> None:
    owner = identity.add_user()
    other = identity.add_user("owner2@example.test")

    result = await identity.deactivate_user.execute(owner.id, other.id)

    assert isinstance(result, Ok)
    assert other.is_active is False


async def test_deactivating_an_unknown_user_is_not_found(identity: Identity) -> None:
    owner = identity.add_user()

    result = await identity.deactivate_user.execute(owner.id, UUID(int=99))

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_USER_NOT_FOUND"


async def test_deactivating_twice_is_idempotent(identity: Identity) -> None:
    owner = identity.add_user()
    staff = identity.add_user(STAFF, role=Role.STAFF)
    await identity.deactivate_user.execute(owner.id, staff.id)

    result = await identity.deactivate_user.execute(owner.id, staff.id)

    assert isinstance(result, Ok)
    assert staff.is_active is False


async def test_reactivating_lets_the_user_sign_in_again(identity: Identity) -> None:
    owner = identity.add_user()
    staff = identity.add_user(STAFF, role=Role.STAFF)
    await identity.deactivate_user.execute(owner.id, staff.id)

    result = await identity.reactivate_user.execute(staff.id)

    assert isinstance(result, Ok)
    assert staff.is_active is True
    assert (await identity.sign_in(STAFF)).user_id == staff.id


async def test_reactivating_an_active_user_is_idempotent(identity: Identity) -> None:
    staff = identity.add_user(STAFF, role=Role.STAFF)

    result = await identity.reactivate_user.execute(staff.id)

    assert isinstance(result, Ok)
    assert staff.is_active is True


async def test_reactivating_an_unknown_user_is_not_found(identity: Identity) -> None:
    result = await identity.reactivate_user.execute(UUID(int=99))

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_USER_NOT_FOUND"


async def test_an_inactive_actor_cannot_deactivate_anyone(identity: Identity) -> None:
    owner = identity.add_user()
    other = identity.add_user("owner2@example.test")
    owner.deactivate()  # e.g. the other owner won the race a moment earlier

    result = await identity.deactivate_user.execute(owner.id, other.id)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_ACTOR_INACTIVE"
    assert other.is_active is True


async def test_an_unknown_actor_cannot_deactivate_anyone(identity: Identity) -> None:
    staff = identity.add_user(STAFF, role=Role.STAFF)
    await identity.sign_in(STAFF)

    result = await identity.deactivate_user.execute(UUID(int=99), staff.id)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_ACTOR_INACTIVE"
    assert staff.is_active is True
    assert all(s.revoked_at is None for s in identity.sessions.by_id.values())


async def test_the_actor_check_comes_before_the_target_lookup(identity: Identity) -> None:
    owner = identity.add_user()
    owner.deactivate()

    result = await identity.deactivate_user.execute(owner.id, UUID(int=99))

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_ACTOR_INACTIVE"


async def test_reactivating_does_not_reopen_old_sessions(identity: Identity) -> None:
    owner = identity.add_user()
    staff = identity.add_user(STAFF, role=Role.STAFF)
    old = await identity.sign_in(STAFF)
    await identity.deactivate_user.execute(owner.id, staff.id)
    await identity.reactivate_user.execute(staff.id)

    assert await identity.resolver.resolve(old.token) is None
