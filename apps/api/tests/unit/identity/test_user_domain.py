from datetime import UTC, datetime

import pytest

from fragancia_api.modules.identity.domain.errors import EmailInvalid, NameInvalid, PasswordTooWeak
from fragancia_api.modules.identity.domain.user import (
    CATALOG_MANAGE,
    ROLE_PERMISSIONS,
    USERS_MANAGE,
    DisplayName,
    Email,
    PlainPassword,
    Role,
    User,
)
from fragancia_api.shared.kernel import Err, Ok

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


def test_email_is_trimmed_and_lowercased() -> None:
    assert Email.create("  Owner@Example.TEST ") == Ok(Email("owner@example.test"))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no-at",
        "a@b@c.com",
        "@example.com",
        "owner@",
        "owner@example",
        "owner@.com",
        "owner@example.",
        "own er@example.com",
        "owner@exa mple.com",
        "a" * 250 + "@x.co",
    ],
)
def test_invalid_emails(raw: str) -> None:
    result = Email.create(raw)
    assert isinstance(result, Err)
    assert isinstance(result.error, EmailInvalid)
    assert result.error.code == "IDENTITY_EMAIL_INVALID"


def test_email_length_limit_is_254_inclusive() -> None:
    at_limit = "a" * (254 - len("@x.co")) + "@x.co"
    assert len(at_limit) == 254
    assert isinstance(Email.create(at_limit), Ok)
    assert isinstance(Email.create("a" + at_limit), Err)


def test_display_name_is_trimmed_and_inner_whitespace_collapsed() -> None:
    assert DisplayName.create("  Dueña   de  Prueba ") == Ok(DisplayName("Dueña de Prueba"))


@pytest.mark.parametrize("raw", ["", " ", "x", "  y  ", "a" * 81])
def test_invalid_display_names(raw: str) -> None:
    result = DisplayName.create(raw)
    assert isinstance(result, Err)
    assert isinstance(result.error, NameInvalid)
    assert result.error.code == "IDENTITY_NAME_INVALID"
    assert result.error.details == {"min": 2, "max": 80}


def test_display_name_length_limits_are_inclusive() -> None:
    assert isinstance(DisplayName.create("ab"), Ok)
    assert isinstance(DisplayName.create("a" * 80), Ok)


def test_password_limits_are_inclusive_and_it_is_kept_exactly_as_typed() -> None:
    padded = "  twelve chars  "
    assert PlainPassword.create(padded) == Ok(PlainPassword(padded))
    assert isinstance(PlainPassword.create("a" * 12), Ok)
    assert isinstance(PlainPassword.create("a" * 128), Ok)


@pytest.mark.parametrize("raw", ["", "a" * 11, "a" * 129])
def test_weak_passwords(raw: str) -> None:
    result = PlainPassword.create(raw)
    assert isinstance(result, Err)
    assert isinstance(result.error, PasswordTooWeak)
    assert result.error.code == "IDENTITY_PASSWORD_TOO_WEAK"
    assert result.error.details == {"min": 12, "max": 128}


def test_password_length_counts_characters_not_bytes() -> None:
    assert isinstance(PlainPassword.create("ñ" * 12), Ok)


def test_a_password_never_shows_in_its_repr() -> None:
    assert repr(PlainPassword("super secret value")) == "PlainPassword(***)"


def test_the_owner_has_every_permission_and_staff_only_the_catalog() -> None:
    assert ROLE_PERMISSIONS[Role.OWNER] == {CATALOG_MANAGE, USERS_MANAGE}
    assert ROLE_PERMISSIONS[Role.STAFF] == {CATALOG_MANAGE}
    assert (CATALOG_MANAGE, USERS_MANAGE) == ("catalog:manage", "users:manage")


def test_role_values_are_the_wire_names() -> None:
    assert (Role.OWNER.value, Role.STAFF.value) == ("owner", "staff")


def _user(role: Role = Role.STAFF) -> User:
    return User.create(Email("a@b.co"), DisplayName("Ana"), role, "hash-1", now=NOW)


def test_new_users_are_active_with_both_timestamps_set_to_now() -> None:
    user = _user()

    assert user.is_active
    assert (user.created_at, user.password_changed_at) == (NOW, NOW)
    assert user.password_hash == "hash-1"


def test_a_user_gets_the_permissions_of_the_role() -> None:
    assert _user(Role.OWNER).permissions == {CATALOG_MANAGE, USERS_MANAGE}
    assert _user(Role.STAFF).permissions == {CATALOG_MANAGE}


def test_changing_the_password_replaces_the_hash_and_stamps_the_time() -> None:
    user = _user()
    later = datetime(2026, 10, 4, tzinfo=UTC)

    user.change_password("hash-2", now=later)

    assert (user.password_hash, user.password_changed_at, user.created_at) == (
        "hash-2",
        later,
        NOW,
    )
