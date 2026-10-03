from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.identity.conftest import PASSWORD, Identity


async def test_creates_an_active_user_with_a_hashed_password(identity: Identity) -> None:
    result = await identity.create_user.execute(
        "  Owner@Example.TEST ", "  Dueña   Prueba ", PASSWORD, Role.OWNER
    )

    assert isinstance(result, Ok)
    user = identity.users.by_id[result.value]
    assert (user.email.value, user.name.value, user.role, user.is_active) == (
        "owner@example.test",
        "Dueña Prueba",
        Role.OWNER,
        True,
    )
    assert user.password_hash == identity.hasher.encode(PASSWORD)
    assert (user.created_at, user.password_changed_at) == (identity.clock.now(),) * 2


async def test_an_email_already_registered_is_a_conflict_ignoring_case(
    identity: Identity,
) -> None:
    await identity.create_user.execute("owner@example.test", "Owner", PASSWORD, Role.OWNER)

    result = await identity.create_user.execute("OWNER@example.test", "Other", PASSWORD, Role.STAFF)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_EMAIL_TAKEN"
    assert len(identity.users.by_id) == 1


async def test_the_first_invalid_value_wins_in_the_order_email_name_password(
    identity: Identity,
) -> None:
    all_bad = await identity.create_user.execute("nope", "x", "short", Role.STAFF)
    name_and_password = await identity.create_user.execute("a@b.co", "x", "short", Role.STAFF)
    password_only = await identity.create_user.execute("a@b.co", "Ana", "short", Role.STAFF)

    assert isinstance(all_bad, Err) and all_bad.error.code == "IDENTITY_EMAIL_INVALID"
    assert isinstance(name_and_password, Err)
    assert name_and_password.error.code == "IDENTITY_NAME_INVALID"
    assert isinstance(password_only, Err)
    assert password_only.error.code == "IDENTITY_PASSWORD_TOO_WEAK"
    assert identity.users.by_id == {}
