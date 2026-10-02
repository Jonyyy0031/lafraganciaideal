"""The Brand aggregate.

Rules: a name is trimmed with inner whitespace collapsed, has 2–80 characters and at least one
letter or digit. Its slug (lowercase ASCII words joined by hyphens) identifies the brand: two
names with the same slug are the same brand ("Maison Margiela" = "maison margiéla").
New brands are active.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Self
from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import BrandNameInvalid
from fragancia_api.shared.kernel import AggregateRoot, DomainEvent, Err, Ok, Result, new_id

NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 80
_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    return _NOT_ALPHANUMERIC.sub("-", ascii_text).strip("-")


@dataclass(frozen=True, slots=True)
class BrandName:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, BrandNameInvalid]:
        value = " ".join(raw.split())
        if not NAME_MIN_LENGTH <= len(value) <= NAME_MAX_LENGTH or not slugify(value):
            return Err(BrandNameInvalid(details={"min": NAME_MIN_LENGTH, "max": NAME_MAX_LENGTH}))
        return Ok(cls(value))

    @property
    def slug(self) -> str:
        return slugify(self.value)


@dataclass(frozen=True, kw_only=True)
class BrandCreated(DomainEvent):
    name: ClassVar[str] = "catalog.brand.created"
    brand_id: UUID
    brand_name: str
    slug: str


class Brand(AggregateRoot):
    def __init__(self, *, id: UUID, name: BrandName, is_active: bool, created_at: datetime) -> None:
        super().__init__()
        self.id = id
        self.name = name
        self.is_active = is_active
        self.created_at = created_at

    @property
    def slug(self) -> str:
        return self.name.slug

    @classmethod
    def create(cls, name: BrandName, *, created_at: datetime) -> Brand:
        brand = cls(id=new_id(), name=name, is_active=True, created_at=created_at)
        brand.record(BrandCreated(brand_id=brand.id, brand_name=name.value, slug=brand.slug))
        return brand
