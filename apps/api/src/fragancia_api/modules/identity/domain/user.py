"""The User aggregate: a back-office account (owner or staff).

Rules: an email is trimmed and lowercased, has at most 254 characters, no whitespace, exactly
one `@`, a non-empty local part and a domain with an inner `.`. A display name is trimmed with
inner whitespace collapsed and has 2–80 characters. A password has 12–128 characters, kept
exactly as typed. Permissions come from the role; new users are active; an owner may
deactivate and reactivate them.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from fragancia_api.modules.identity.domain.errors import EmailInvalid, NameInvalid, PasswordTooWeak
from fragancia_api.shared.kernel import AggregateRoot, Err, Ok, Result, new_id

EMAIL_MAX_LENGTH = 254
NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 80
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


@dataclass(frozen=True, slots=True)
class Email:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, EmailInvalid]:
        value = raw.strip().lower()
        local, at, domain = value.partition("@")
        valid = (
            len(value) <= EMAIL_MAX_LENGTH
            and not any(char.isspace() for char in value)
            and value.count("@") == 1
            and bool(at)
            and bool(local)
            and "." in domain[1:-1]
        )
        if not valid:
            return Err(EmailInvalid())
        return Ok(cls(value))


@dataclass(frozen=True, slots=True)
class DisplayName:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, NameInvalid]:
        value = " ".join(raw.split())
        if not NAME_MIN_LENGTH <= len(value) <= NAME_MAX_LENGTH:
            return Err(NameInvalid(details={"min": NAME_MIN_LENGTH, "max": NAME_MAX_LENGTH}))
        return Ok(cls(value))


@dataclass(frozen=True, slots=True, repr=False)
class PlainPassword:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, PasswordTooWeak]:
        if not PASSWORD_MIN_LENGTH <= len(raw) <= PASSWORD_MAX_LENGTH:
            return Err(
                PasswordTooWeak(details={"min": PASSWORD_MIN_LENGTH, "max": PASSWORD_MAX_LENGTH})
            )
        return Ok(cls(raw))

    def __repr__(self) -> str:
        return "PlainPassword(***)"


class Role(StrEnum):
    OWNER = "owner"
    STAFF = "staff"


CATALOG_MANAGE = "catalog:manage"
USERS_MANAGE = "users:manage"

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OWNER: frozenset({CATALOG_MANAGE, USERS_MANAGE}),
    Role.STAFF: frozenset({CATALOG_MANAGE}),
}


class User(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        email: Email,
        name: DisplayName,
        role: Role,
        password_hash: str,
        is_active: bool,
        created_at: datetime,
        password_changed_at: datetime,
    ) -> None:
        super().__init__()
        self.id = id
        self.email = email
        self.name = name
        self.role = role
        self.password_hash = password_hash
        self.is_active = is_active
        self.created_at = created_at
        self.password_changed_at = password_changed_at

    @classmethod
    def create(
        cls, email: Email, name: DisplayName, role: Role, password_hash: str, *, now: datetime
    ) -> User:
        return cls(
            id=new_id(),
            email=email,
            name=name,
            role=role,
            password_hash=password_hash,
            is_active=True,
            created_at=now,
            password_changed_at=now,
        )

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS[self.role]

    def change_password(self, new_hash: str, *, now: datetime) -> None:
        self.password_hash = new_hash
        self.password_changed_at = now

    def deactivate(self) -> None:
        self.is_active = False

    def reactivate(self) -> None:
        self.is_active = True
