"""Regression tests for plan 001, repair round 1, L2 (README decision 8): checking the current
password is throttled per user, counted before the check."""

from uuid import UUID

from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.identity.conftest import PASSWORD, Identity

NEW_PASSWORD = "a brand new passphrase"
WRONG = "not it at all"


async def _open(identity: Identity, email: str = "owner@example.test") -> UUID:
    login = await identity.sign_in(email)
    session = await identity.sessions.get_by_token_hash(identity.tokens.digest(login.token))
    assert session is not None
    return session.id


def _key(user_id: UUID) -> str:
    return f"password:{user_id}"


async def test_a_wrong_current_password_is_counted_under_the_users_key(
    identity: Identity,
) -> None:
    user = identity.add_user()
    current = await _open(identity)

    await identity.change_password.execute(user.id, current, WRONG, NEW_PASSWORD)
    await identity.change_password.execute(user.id, current, WRONG, NEW_PASSWORD)

    assert identity.throttle.counts[_key(user.id)][0] == 2


async def test_the_attempt_above_the_limit_is_rejected_without_verifying_the_password(
    identity: Identity,
) -> None:
    user = identity.add_user()
    current = await _open(identity)
    for _ in range(identity.policy.email_max_attempts):
        await identity.change_password.execute(user.id, current, WRONG, NEW_PASSWORD)
    verified_before = len(identity.hasher.verified_hashes)

    result = await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_TOO_MANY_ATTEMPTS"
    assert len(identity.hasher.verified_hashes) == verified_before  # not even the right one
    assert user.password_hash == identity.hasher.encode(PASSWORD)


async def test_the_last_attempt_within_the_limit_still_works(identity: Identity) -> None:
    user = identity.add_user()
    current = await _open(identity)
    for _ in range(identity.policy.email_max_attempts - 1):
        await identity.change_password.execute(user.id, current, WRONG, NEW_PASSWORD)

    result = await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert isinstance(result, Ok)


async def test_a_correct_password_clears_the_count(identity: Identity) -> None:
    user = identity.add_user()
    current = await _open(identity)
    await identity.change_password.execute(user.id, current, WRONG, NEW_PASSWORD)

    await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert _key(user.id) not in identity.throttle.counts


async def test_a_weak_new_password_is_not_counted(identity: Identity) -> None:
    user = identity.add_user()
    current = await _open(identity)

    await identity.change_password.execute(user.id, current, WRONG, "short")

    assert _key(user.id) not in identity.throttle.counts


async def test_the_count_belongs_to_the_user_who_changes_the_password(
    identity: Identity,
) -> None:
    user = identity.add_user()
    colleague = identity.add_user("staff@example.test", role=Role.STAFF)
    current = await _open(identity)

    await identity.change_password.execute(user.id, current, WRONG, NEW_PASSWORD)

    assert _key(user.id) in identity.throttle.counts
    assert _key(colleague.id) not in identity.throttle.counts


async def test_the_counter_starts_over_after_the_window(identity: Identity) -> None:
    user = identity.add_user()
    current = await _open(identity)
    for _ in range(identity.policy.email_max_attempts):
        await identity.change_password.execute(user.id, current, WRONG, NEW_PASSWORD)
    identity.clock.current += identity.policy.throttle_window

    result = await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert isinstance(result, Ok)
