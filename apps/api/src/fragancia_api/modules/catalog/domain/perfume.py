"""The Perfume aggregate and its Presentation child entities.

Perfume fields and rules:

- `name`: the brand name rules (trimmed with inner whitespace collapsed, 2-80 characters, at
  least one letter or digit, slug of at most 100 characters).
- `gender`: women, men or unisex.
- `description`: trimmed, 0-2000 characters (optional).
- `top_notes`, `heart_notes`, `base_notes`: each at most 10 notes; a note is trimmed with inner
  whitespace collapsed and has 1-40 characters (an empty note is invalid). Order and duplicates
  are kept.
- `brand_id`, `concentration_id`, `family_id`: references to other catalog lists.
- `slug`: slugify("<brand name> <perfume name> <concentration abbreviation>"), stored and
  recomputed on every create and update; renaming the brand or the concentration later does not
  change it.
- `is_published`, `first_published_at` (null until the first publish, never cleared),
  `is_archived`, `created_at`, `updated_at`.

Identity: brand + perfume name slug + concentration; a duplicate is a conflict, and so is a clash
on the stored slug.

References: on create the brand, family and concentration must exist and be active; on update
only a changed reference must be active (an unchanged archived one is kept). The use cases check
this, since the aggregate does not see the other lists.

State: new perfumes are hidden and not archived. Publishing needs a non-archived perfume with at
least one active presentation, and sets `first_published_at` only when it is null; publishing a
published perfume is a no-op. Hiding is idempotent. Archiving also hides and is idempotent;
restoring leaves the perfume hidden. An archived perfume is read-only: update, publish and every
presentation change are refused; only restore and hide work on it.

Presentation fields and rules:

- `ml`: an integer from 1 to 1000, unique within the perfume (archived presentations included).
- `price`: 1 to 10,000,000 cents (MXN, VAT included).
- `sale`: optional price + `starts_at` + `ends_at`. The sale price is 1 cent or more and lower
  than the regular price; each date is optional and timezone-aware, and with both, `ends_at` is
  after `starts_at`. Past windows are allowed. Dates without a sale price are invalid.
- `availability`: `in_stock` with no lead time, or `made_to_order` with a lead time of 1-90 days
  (min <= max).
- `is_active`, `created_at`.

Presentation operations: adding or updating to an ml another presentation has is a conflict.
Archiving the last active presentation of a published perfume is a conflict (hide the perfume
first). Archiving and restoring a presentation are idempotent. Nothing is ever deleted.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import (
    PerfumeArchived,
    PerfumeDescriptionTooLong,
    PerfumeLastPresentation,
    PerfumeNameInvalid,
    PerfumeNotesInvalid,
    PerfumeNothingToSell,
    PresentationAlreadyExists,
    PresentationAvailabilityInvalid,
    PresentationMlInvalid,
    PresentationNotFound,
    PresentationPriceInvalid,
    PresentationSaleInvalid,
)
from fragancia_api.modules.catalog.domain.naming import (
    NAME_MAX_LENGTH,
    NAME_MIN_LENGTH,
    clean_name,
    slugify,
)
from fragancia_api.shared.kernel import AggregateRoot, Err, Money, Ok, Result, new_id

DESCRIPTION_MAX_LENGTH = 2000
NOTES_MAX_PER_LEVEL = 10
NOTE_MAX_LENGTH = 40
ML_MIN = 1
ML_MAX = 1000
PRICE_MIN_CENTS = 1
PRICE_MAX_CENTS = 10_000_000  # $100,000 MXN
LEAD_TIME_MIN_DAYS = 1
LEAD_TIME_MAX_DAYS = 90

type AvailabilityKind = Literal["in_stock", "made_to_order"]


class Gender(StrEnum):
    WOMEN = "women"
    MEN = "men"
    UNISEX = "unisex"


@dataclass(frozen=True, slots=True)
class PerfumeName:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, PerfumeNameInvalid]:
        value = clean_name(raw)
        if value is None:
            return Err(PerfumeNameInvalid(details={"min": NAME_MIN_LENGTH, "max": NAME_MAX_LENGTH}))
        return Ok(cls(value))

    @property
    def slug(self) -> str:
        return slugify(self.value)


@dataclass(frozen=True, slots=True)
class Description:
    value: str

    @classmethod
    def create(cls, raw: str) -> Result[Self, PerfumeDescriptionTooLong]:
        value = raw.strip()
        if len(value) > DESCRIPTION_MAX_LENGTH:
            return Err(PerfumeDescriptionTooLong(details={"max": DESCRIPTION_MAX_LENGTH}))
        return Ok(cls(value))


@dataclass(frozen=True, slots=True)
class Notes:
    top: tuple[str, ...]
    heart: tuple[str, ...]
    base: tuple[str, ...]

    @classmethod
    def create(
        cls, top: list[str], heart: list[str], base: list[str]
    ) -> Result[Self, PerfumeNotesInvalid]:
        levels: list[tuple[str, ...]] = []
        for raw_notes in (top, heart, base):
            if len(raw_notes) > NOTES_MAX_PER_LEVEL:
                return Err(_notes_invalid())
            notes = tuple(" ".join(raw.split()) for raw in raw_notes)
            if any(not 1 <= len(note) <= NOTE_MAX_LENGTH for note in notes):
                return Err(_notes_invalid())
            levels.append(notes)
        return Ok(cls(levels[0], levels[1], levels[2]))


def _notes_invalid() -> PerfumeNotesInvalid:
    return PerfumeNotesInvalid(
        details={"max_per_level": NOTES_MAX_PER_LEVEL, "max_length": NOTE_MAX_LENGTH}
    )


@dataclass(frozen=True, slots=True)
class Ml:
    value: int

    @classmethod
    def create(cls, value: int) -> Result[Self, PresentationMlInvalid]:
        if not ML_MIN <= value <= ML_MAX:
            return Err(PresentationMlInvalid(details={"min": ML_MIN, "max": ML_MAX}))
        return Ok(cls(value))


@dataclass(frozen=True, slots=True)
class Price:
    amount: Money

    @classmethod
    def create(cls, cents: int) -> Result[Self, PresentationPriceInvalid]:
        if not PRICE_MIN_CENTS <= cents <= PRICE_MAX_CENTS:
            return Err(
                PresentationPriceInvalid(
                    details={"min_cents": PRICE_MIN_CENTS, "max_cents": PRICE_MAX_CENTS}
                )
            )
        return Ok(cls(Money(cents)))


@dataclass(frozen=True, slots=True)
class Sale:
    price: Money
    starts_at: datetime | None
    ends_at: datetime | None

    @classmethod
    def create(
        cls,
        price_cents: int | None,
        starts_at: datetime | None,
        ends_at: datetime | None,
        *,
        regular: Price,
    ) -> Result[Self | None, PresentationSaleInvalid]:
        """The sale, or None when there is none (no price and no dates)."""
        if price_cents is None:
            if starts_at is not None or ends_at is not None:
                return Err(PresentationSaleInvalid())
            return Ok(None)
        if not 1 <= price_cents < regular.amount.cents:
            return Err(PresentationSaleInvalid())
        if any(moment is not None and moment.tzinfo is None for moment in (starts_at, ends_at)):
            return Err(PresentationSaleInvalid())
        if starts_at is not None and ends_at is not None and ends_at <= starts_at:
            return Err(PresentationSaleInvalid())
        return Ok(cls(Money(price_cents), starts_at, ends_at))


@dataclass(frozen=True, slots=True)
class Availability:
    kind: AvailabilityKind
    min_days: int | None
    max_days: int | None

    @classmethod
    def create(
        cls, kind: AvailabilityKind, min_days: int | None, max_days: int | None
    ) -> Result[Self, PresentationAvailabilityInvalid]:
        if kind == "in_stock":
            if min_days is not None or max_days is not None:
                return Err(_availability_invalid())
            return Ok(cls(kind, None, None))
        if min_days is None or max_days is None:
            return Err(_availability_invalid())
        if not LEAD_TIME_MIN_DAYS <= min_days <= max_days <= LEAD_TIME_MAX_DAYS:
            return Err(_availability_invalid())
        return Ok(cls(kind, min_days, max_days))


def _availability_invalid() -> PresentationAvailabilityInvalid:
    return PresentationAvailabilityInvalid(
        details={"min_days": LEAD_TIME_MIN_DAYS, "max_days": LEAD_TIME_MAX_DAYS}
    )


def perfume_slug(brand_name: str, perfume_name: PerfumeName, abbreviation: str) -> str:
    """The storefront slug: brand + perfume name + concentration abbreviation."""
    return slugify(f"{brand_name} {perfume_name.value} {abbreviation}")


class Presentation:
    """A size of a perfume, with its price, optional sale and availability."""

    def __init__(
        self,
        *,
        id: UUID,
        ml: Ml,
        price: Price,
        sale: Sale | None,
        availability: Availability,
        is_active: bool,
        created_at: datetime,
    ) -> None:
        self.id = id
        self.ml = ml
        self.price = price
        self.sale = sale
        self.availability = availability
        self.is_active = is_active
        self.created_at = created_at


class Perfume(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        brand_id: UUID,
        concentration_id: UUID,
        family_id: UUID,
        name: PerfumeName,
        slug: str,
        gender: Gender,
        description: Description,
        notes: Notes,
        is_published: bool,
        first_published_at: datetime | None,
        is_archived: bool,
        created_at: datetime,
        updated_at: datetime,
        presentations: list[Presentation],
    ) -> None:
        super().__init__()
        self.id = id
        self.brand_id = brand_id
        self.concentration_id = concentration_id
        self.family_id = family_id
        self.name = name
        self.slug = slug
        self.gender = gender
        self.description = description
        self.notes = notes
        self.is_published = is_published
        self.first_published_at = first_published_at
        self.is_archived = is_archived
        self.created_at = created_at
        self.updated_at = updated_at
        self.presentations = presentations

    @property
    def name_slug(self) -> str:
        return self.name.slug

    @classmethod
    def create(
        cls,
        *,
        brand_id: UUID,
        concentration_id: UUID,
        family_id: UUID,
        name: PerfumeName,
        slug: str,
        gender: Gender,
        description: Description,
        notes: Notes,
        now: datetime,
    ) -> Perfume:
        """A new perfume: hidden, not archived, without presentations."""
        return cls(
            id=new_id(),
            brand_id=brand_id,
            concentration_id=concentration_id,
            family_id=family_id,
            name=name,
            slug=slug,
            gender=gender,
            description=description,
            notes=notes,
            is_published=False,
            first_published_at=None,
            is_archived=False,
            created_at=now,
            updated_at=now,
            presentations=[],
        )

    def update(
        self,
        *,
        brand_id: UUID,
        concentration_id: UUID,
        family_id: UUID,
        name: PerfumeName,
        slug: str,
        gender: Gender,
        description: Description,
        notes: Notes,
        now: datetime,
    ) -> Result[None, PerfumeArchived]:
        """Replace every field (the use case recomputes the slug)."""
        if self.is_archived:
            return Err(PerfumeArchived())
        self.brand_id = brand_id
        self.concentration_id = concentration_id
        self.family_id = family_id
        self.name = name
        self.slug = slug
        self.gender = gender
        self.description = description
        self.notes = notes
        self.updated_at = now
        return Ok(None)

    def publish(self, now: datetime) -> Result[None, PerfumeArchived | PerfumeNothingToSell]:
        if self.is_archived:
            return Err(PerfumeArchived())
        if not any(presentation.is_active for presentation in self.presentations):
            return Err(PerfumeNothingToSell())
        self.is_published = True
        if self.first_published_at is None:
            self.first_published_at = now
        return Ok(None)

    def hide(self) -> None:
        self.is_published = False

    def archive(self) -> None:
        self.is_archived = True
        self.is_published = False

    def restore(self) -> None:
        self.is_archived = False

    def add_presentation(
        self,
        ml: Ml,
        price: Price,
        sale: Sale | None,
        availability: Availability,
        *,
        created_at: datetime,
    ) -> Result[UUID, PerfumeArchived | PresentationAlreadyExists]:
        if self.is_archived:
            return Err(PerfumeArchived())
        if any(presentation.ml == ml for presentation in self.presentations):
            return Err(PresentationAlreadyExists())
        presentation = Presentation(
            id=new_id(),
            ml=ml,
            price=price,
            sale=sale,
            availability=availability,
            is_active=True,
            created_at=created_at,
        )
        self.presentations.append(presentation)
        self.updated_at = created_at
        return Ok(presentation.id)

    def update_presentation(
        self,
        presentation_id: UUID,
        ml: Ml,
        price: Price,
        sale: Sale | None,
        availability: Availability,
        *,
        now: datetime,
    ) -> Result[None, PerfumeArchived | PresentationNotFound | PresentationAlreadyExists]:
        """Replace ml, price, sale and availability of one presentation."""
        if self.is_archived:
            return Err(PerfumeArchived())
        presentation = self._presentation(presentation_id)
        if presentation is None:
            return Err(PresentationNotFound())
        if any(other.ml == ml and other.id != presentation_id for other in self.presentations):
            return Err(PresentationAlreadyExists())
        presentation.ml = ml
        presentation.price = price
        presentation.sale = sale
        presentation.availability = availability
        self.updated_at = now
        return Ok(None)

    def archive_presentation(
        self, presentation_id: UUID
    ) -> Result[None, PerfumeArchived | PresentationNotFound | PerfumeLastPresentation]:
        if self.is_archived:
            return Err(PerfumeArchived())
        presentation = self._presentation(presentation_id)
        if presentation is None:
            return Err(PresentationNotFound())
        if not presentation.is_active:
            return Ok(None)
        others_active = any(
            other.is_active for other in self.presentations if other.id != presentation_id
        )
        if self.is_published and not others_active:
            return Err(PerfumeLastPresentation())
        presentation.is_active = False
        return Ok(None)

    def restore_presentation(
        self, presentation_id: UUID
    ) -> Result[None, PerfumeArchived | PresentationNotFound]:
        if self.is_archived:
            return Err(PerfumeArchived())
        presentation = self._presentation(presentation_id)
        if presentation is None:
            return Err(PresentationNotFound())
        presentation.is_active = True
        return Ok(None)

    def _presentation(self, presentation_id: UUID) -> Presentation | None:
        return next((p for p in self.presentations if p.id == presentation_id), None)
