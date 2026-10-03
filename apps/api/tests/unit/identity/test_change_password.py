from uuid import UUID, uuid4

import pytest

from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.identity.conftest import PASSWORD, Identity

NEW_PASSWORD = "a brand new passphrase"


async def _open(identity: Identity, email: str = "owner@example.test") -> UUID:
    login = await identity.sign_in(email)
    session = await identity.sessions.get_by_token_hash(identity.tokens.digest(login.token))
    assert session is not None
    return session.id


async def test_changes_the_password_and_stamps_the_time(identity: Identity) -> None:
    user = identity.add_user()
    current = await _open(identity)
    identity.clock.current += identity.policy.session_idle / 4

    result = await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert isinstance(result, Ok)
    assert user.password_hash == identity.hasher.hash(NEW_PASSWORD)
    assert user.password_changed_at == identity.clock.now()


async def test_closes_every_other_session_of_the_user_and_keeps_the_current_one(
    identity: Identity,
) -> None:
    user = identity.add_user()
    current = await _open(identity)
    other = await _open(identity)

    await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert identity.sessions.by_id[current].revoked_at is None
    assert identity.sessions.by_id[other].revoked_at == identity.clock.now()


async def test_does_not_touch_the_sessions_of_other_users(identity: Identity) -> None:
    user = identity.add_user()
    identity.add_user("staff@example.test", role=Role.STAFF)
    current = await _open(identity)
    colleague = await _open(identity, "staff@example.test")

    await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert identity.sessions.by_id[colleague].revoked_at is None


async def test_a_wrong_current_password_changes_and_closes_nothing(identity: Identity) -> None:
    user = identity.add_user()
    current = await _open(identity)
    other = await _open(identity)
    old_hash = user.password_hash

    result = await identity.change_password.execute(user.id, current, "not it at all", NEW_PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_CURRENT_PASSWORD_WRONG"
    assert user.password_hash == old_hash
    assert identity.sessions.by_id[other].revoked_at is None


async def test_a_weak_new_password_is_rejected_before_the_current_one_is_checked(
    identity: Identity,
) -> None:
    user = identity.add_user()
    current = await _open(identity)
    verified_before = len(identity.hasher.verified_hashes)

    result = await identity.change_password.execute(user.id, current, "wrong", "short")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_PASSWORD_TOO_WEAK"
    assert len(identity.hasher.verified_hashes) == verified_before


async def test_the_new_password_works_for_the_next_sign_in_and_the_old_one_does_not(
    identity: Identity,
) -> None:
    user = identity.add_user()
    current = await _open(identity)
    await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    old = await identity.log_in.execute("owner@example.test", PASSWORD, ip=None, user_agent=None)
    new = await identity.log_in.execute(
        "owner@example.test", NEW_PASSWORD, ip=None, user_agent=None
    )

    assert isinstance(old, Err)
    assert isinstance(new, Ok)


async def test_a_user_that_does_not_exist_is_an_unexpected_error(identity: Identity) -> None:
    with pytest.raises(LookupError):
        await identity.change_password.execute(uuid4(), uuid4(), PASSWORD, NEW_PASSWORD)
