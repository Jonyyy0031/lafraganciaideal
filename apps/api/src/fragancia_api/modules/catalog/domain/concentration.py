"""The Concentration aggregate (Eau de Toilette, Eau de Parfum...).

Rules: a name follows the brand rules exactly: trimmed with inner whitespace collapsed, 2-80
characters, at least one letter or digit. An abbreviation ("EDT") is trimmed with inner
whitespace collapsed, 2-12 characters, at least one letter or digit, slug of at most 20
characters; it is kept as typed. The name slug identifies the concentration and the abbreviation
slug is its short form in URLs: both are unique, across active and archived concentrations
("EDT" and "edt" clash). New concentrations are active; an update changes both texts at once (the
slugs follow); concentrations are archived and restored, never deleted. Archiving and restoring
are idempotent.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Self
from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import (
    ConcentrationAbbreviationInvalid,
    ConcentrationNameInvalid,
)
from fragancia_api.modules.catalog.domain.naming import (
    ABBREVIATION_MAX_LENGTH,
    ABBREVIATION_MIN_LENGTH,
    NAME_MAX_LENGTH,
    NAME_MIN_LENGTH,
    clean_abbreviation,
    clean_name,
    slugify,
)
from fragancia_api.shared.kernel import AggregateRoot, Err, Ok, Result, new_id


@dataclass(frozen=True, slots=True)
class ConcentrationName:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, ConcentrationNameInvalid]:
        value = clean_name(raw)
        if value is None:
            return Err(
                ConcentrationNameInvalid(details={"min": NAME_MIN_LENGTH, "max": NAME_MAX_LENGTH})
            )
        return Ok(cls(value))

    @property
    def slug(self) -> str:
        return slugify(self.value)


@dataclass(frozen=True, slots=True)
class Abbreviation:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, ConcentrationAbbreviationInvalid]:
        value = clean_abbreviation(raw)
        if value is None:
            return Err(
                ConcentrationAbbreviationInvalid(
                    details={"min": ABBREVIATION_MIN_LENGTH, "max": ABBREVIATION_MAX_LENGTH}
                )
            )
        return Ok(cls(value))

    @property
    def slug(self) -> str:
        return slugify(self.value)


class Concentration(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        name: ConcentrationName,
        abbreviation: Abbreviation,
        is_active: bool,
        created_at: datetime,
    ) -> None:
        super().__init__()
        self.id = id
        self.name = name
        self.abbreviation = abbreviation
        self.is_active = is_active
        self.created_at = created_at

    @property
    def slug(self) -> str:
        return self.name.slug

    @property
    def abbreviation_slug(self) -> str:
        return self.abbreviation.slug

    @classmethod
    def create(
        cls, name: ConcentrationName, abbreviation: Abbreviation, *, created_at: datetime
    ) -> Concentration:
        return cls(
            id=new_id(),
            name=name,
            abbreviation=abbreviation,
            is_active=True,
            created_at=created_at,
        )

    def update(self, name: ConcentrationName, abbreviation: Abbreviation) -> None:
        self.name = name
        self.abbreviation = abbreviation

    def archive(self) -> None:
        self.is_active = False

    def restore(self) -> None:
        self.is_active = True
