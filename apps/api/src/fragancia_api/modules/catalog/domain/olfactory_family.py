"""The OlfactoryFamily aggregate (woody, floral, citrus...).

Rules: a name follows the brand rules exactly: trimmed with inner whitespace collapsed, 2-80
characters, at least one letter or digit. Its slug identifies the family: two names with the same
slug are the same family. Slugs stay unique across active and archived families. New families are
active; families are renamed (the slug follows), archived and restored, never deleted. Archiving
and restoring are idempotent.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Self
from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import FamilyNameInvalid
from fragancia_api.modules.catalog.domain.naming import (
    NAME_MAX_LENGTH,
    NAME_MIN_LENGTH,
    clean_name,
    slugify,
)
from fragancia_api.shared.kernel import AggregateRoot, Err, Ok, Result, new_id


@dataclass(frozen=True, slots=True)
class FamilyName:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, FamilyNameInvalid]:
        value = clean_name(raw)
        if value is None:
            return Err(FamilyNameInvalid(details={"min": NAME_MIN_LENGTH, "max": NAME_MAX_LENGTH}))
        return Ok(cls(value))

    @property
    def slug(self) -> str:
        return slugify(self.value)


class OlfactoryFamily(AggregateRoot):
    def __init__(
        self, *, id: UUID, name: FamilyName, is_active: bool, created_at: datetime
    ) -> None:
        super().__init__()
        self.id = id
        self.name = name
        self.is_active = is_active
        self.created_at = created_at

    @property
    def slug(self) -> str:
        return self.name.slug

    @classmethod
    def create(cls, name: FamilyName, *, created_at: datetime) -> OlfactoryFamily:
        return cls(id=new_id(), name=name, is_active=True, created_at=created_at)

    def rename(self, name: FamilyName) -> None:
        self.name = name

    def archive(self) -> None:
        self.is_active = False

    def restore(self) -> None:
        self.is_active = True
