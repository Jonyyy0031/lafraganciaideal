from datetime import timedelta
from uuid import UUID, uuid4

from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.identity.conftest import Identity


async def _open(identity: Identity, email: str = "owner@example.test") -> UUID:
    login = await identity.sign_in(email)
    session = await identity.sessions.get_by_token_hash(identity.tokens.digest(login.token))
    assert session is not None
    return session.id


async def test_log_out_revokes_the_session(identity: Identity) -> None:
    identity.add_user()
    session_id = await _open(identity)

    result = await identity.log_out.execute(session_id)

    assert isinstance(result, Ok)
    assert identity.sessions.by_id[session_id].revoked_at == identity.clock.now()


async def test_log_out_of_an_unknown_session_still_succeeds(identity: Identity) -> None:
    assert isinstance(await identity.log_out.execute(uuid4()), Ok)


async def test_a_revoked_session_can_no_longer_be_resolved(identity: Identity) -> None:
    identity.add_user()
    login = await identity.sign_in()
    session_id = (await identity.resolver.resolve(login.token)).session_id  # type: ignore[union-attr]
    assert session_id is not None

    await identity.log_out.execute(session_id)

    assert await identity.resolver.resolve(login.token) is None


async def test_closes_one_of_my_sessions(identity: Identity) -> None:
    user = identity.add_user()
    current = await _open(identity)
    other = await _open(identity)

    result = await identity.revoke_session.execute(user.id, other)

    assert isinstance(result, Ok)
    assert identity.sessions.by_id[other].revoked_at == identity.clock.now()
    assert identity.sessions.by_id[current].revoked_at is None


async def test_another_users_session_is_not_found_and_stays_open(identity: Identity) -> None:
    user = identity.add_user()
    identity.add_user("staff@example.test", role=Role.STAFF)
    theirs = await _open(identity, "staff@example.test")

    result = await identity.revoke_session.execute(user.id, theirs)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_SESSION_NOT_FOUND"
    assert identity.sessions.by_id[theirs].revoked_at is None


async def test_an_unknown_session_is_not_found(identity: Identity) -> None:
    user = identity.add_user()

    result = await identity.revoke_session.execute(user.id, uuid4())

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_SESSION_NOT_FOUND"


async def test_an_already_closed_session_is_not_found(identity: Identity) -> None:
    user = identity.add_user()
    other = await _open(identity)
    await identity.revoke_session.execute(user.id, other)

    result = await identity.revoke_session.execute(user.id, other)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_SESSION_NOT_FOUND"


async def test_closing_the_other_sessions_keeps_the_current_one(identity: Identity) -> None:
    user = identity.add_user()
    identity.add_user("staff@example.test", role=Role.STAFF)
    current = await _open(identity)
    other = await _open(identity)
    colleague = await _open(identity, "staff@example.test")

    result = await identity.revoke_others.execute(user.id, current)

    assert isinstance(result, Ok)
    assert identity.sessions.by_id[current].revoked_at is None
    assert identity.sessions.by_id[other].revoked_at == identity.clock.now()
    assert identity.sessions.by_id[colleague].revoked_at is None


async def test_my_account_shows_the_role_and_sorted_permissions(identity: Identity) -> None:
    owner = identity.add_user()
    staff = identity.add_user("staff@example.test", role=Role.STAFF)

    owner_me = await identity.get_my_account.execute(owner.id)
    staff_me = await identity.get_my_account.execute(staff.id)

    assert owner_me is not None and staff_me is not None
    assert (owner_me.email, owner_me.role, owner_me.permissions) == (
        "owner@example.test",
        "owner",
        ["catalog:manage", "users:manage"],
    )
    assert (staff_me.role, staff_me.permissions) == ("staff", ["catalog:manage"])
    assert await identity.get_my_account.execute(uuid4()) is None


async def test_my_sessions_lists_only_valid_ones_newest_first_and_marks_the_current(
    identity: Identity,
) -> None:
    user = identity.add_user()
    identity.add_user("staff@example.test", role=Role.STAFF)
    old = await _open(identity)
    identity.clock.current += timedelta(minutes=10)
    current = await _open(identity)
    closed = await _open(identity)
    await identity.revoke_session.execute(user.id, closed)
    await _open(identity, "staff@example.test")  # someone else's

    listed = await identity.list_my_sessions.execute(user.id, current_session_id=current)

    assert [s.id for s in listed] == [current, old]
    assert [s.current for s in listed] == [True, False]


async def test_my_sessions_leaves_out_idle_and_expired_ones(identity: Identity) -> None:
    user = identity.add_user()
    await _open(identity)
    identity.clock.current += identity.policy.session_idle  # idle deadline reached
    fresh = await _open(identity)

    listed = await identity.list_my_sessions.execute(user.id, current_session_id=fresh)

    assert [s.id for s in listed] == [fresh]
