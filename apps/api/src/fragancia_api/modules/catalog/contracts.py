"""HTTP contracts of the catalog (source of truth for OpenAPI and the generated web client)."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from fragancia_api.shared.contracts import Page


class CreateBrandRequest(BaseModel):
    # Business rules (length, characters) live in the domain; this only bounds the payload.
    name: str = Field(max_length=200, examples=["Maison Margiela"])


class RenameBrandRequest(BaseModel):
    # Business rules (length, characters) live in the domain; this only bounds the payload.
    name: str = Field(max_length=200, examples=["Dior"])


class CreatedResponse(BaseModel):
    id: UUID


class PublicBrand(BaseModel):
    """A brand as customers see it (active brands only)."""

    id: UUID
    name: str
    slug: str


class AdminBrand(BaseModel):
    """A brand as the back office sees it."""

    id: UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime


class AdminBrandPage(Page[AdminBrand]):
    """One page of the admin brand list."""


class CreateOlfactoryFamilyRequest(BaseModel):
    """Payload to register an olfactory family."""

    name: str = Field(max_length=200, examples=["Amaderada"])


class RenameOlfactoryFamilyRequest(BaseModel):
    """Payload to rename an olfactory family."""

    name: str = Field(max_length=200, examples=["Amaderada"])


class PublicOlfactoryFamily(BaseModel):
    """An olfactory family as customers see it (active families only)."""

    id: UUID
    name: str
    slug: str


class AdminOlfactoryFamily(BaseModel):
    """An olfactory family as the back office sees it."""

    id: UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime


class AdminOlfactoryFamilyPage(Page[AdminOlfactoryFamily]):
    """One page of the admin olfactory family list."""


class CreateConcentrationRequest(BaseModel):
    """Payload to register a concentration."""

    # Business rules (length, characters) live in the domain; this only bounds the payload.
    name: str = Field(max_length=200, examples=["Eau de Toilette"])
    abbreviation: str = Field(max_length=50, examples=["EDT"])


class UpdateConcentrationRequest(BaseModel):
    """Payload to change both texts of a concentration (both required)."""

    # Business rules (length, characters) live in the domain; this only bounds the payload.
    name: str = Field(max_length=200, examples=["Eau de Toilette"])
    abbreviation: str = Field(max_length=50, examples=["EDT"])


class PublicConcentration(BaseModel):
    """A concentration as customers see it (active ones only); `slug` is the name slug."""

    id: UUID
    name: str
    abbreviation: str
    slug: str


class AdminConcentration(BaseModel):
    """A concentration as the back office sees it."""

    id: UUID
    name: str
    abbreviation: str
    slug: str
    is_active: bool
    created_at: datetime


class AdminConcentrationPage(Page[AdminConcentration]):
    """One page of the admin concentration list."""


class PerfumeRequest(BaseModel):
    """Payload to create a perfume, or to replace every field of one (update)."""

    # Business rules (lengths, counts, characters) live in the domain; this only bounds the
    # payload.
    brand_id: UUID
    concentration_id: UUID
    family_id: UUID
    name: str = Field(max_length=200, examples=["Eros"])
    gender: Literal["women", "men", "unisex"]
    description: str = Field(default="", max_length=4000)
    top_notes: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=50
    )
    heart_notes: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=50
    )
    base_notes: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=50
    )


class PresentationRequest(BaseModel):
    """Payload to add a presentation, or to replace every field of one (update)."""

    # Business rules (ranges, sale window, lead times) live in the domain.
    ml: int = Field(examples=[100])
    price_cents: int = Field(examples=[250000])
    sale_price_cents: int | None = None
    sale_starts_at: AwareDatetime | None = None
    sale_ends_at: AwareDatetime | None = None
    availability: Literal["in_stock", "made_to_order"]
    lead_time_min_days: int | None = None
    lead_time_max_days: int | None = None


class AdminPresentation(BaseModel):
    """A presentation (size) of a perfume as the back office sees it."""

    id: UUID
    ml: int
    price_cents: int
    sale_price_cents: int | None
    sale_starts_at: datetime | None
    sale_ends_at: datetime | None
    availability: Literal["in_stock", "made_to_order"]
    lead_time_min_days: int | None
    lead_time_max_days: int | None
    is_active: bool
    created_at: datetime


class PerfumeBrandRef(BaseModel):
    """The brand of a perfume."""

    id: UUID
    name: str
    is_active: bool


class PerfumeConcentrationRef(BaseModel):
    """The concentration of a perfume."""

    id: UUID
    name: str
    abbreviation: str
    is_active: bool


class PerfumeFamilyRef(BaseModel):
    """The olfactory family of a perfume."""

    id: UUID
    name: str
    is_active: bool


class AdminPerfume(BaseModel):
    """A perfume as the back office sees it, with every presentation ordered by ml."""

    id: UUID
    slug: str
    name: str
    gender: Literal["women", "men", "unisex"]
    description: str
    top_notes: list[str]
    heart_notes: list[str]
    base_notes: list[str]
    brand: PerfumeBrandRef
    concentration: PerfumeConcentrationRef
    family: PerfumeFamilyRef
    is_published: bool
    first_published_at: datetime | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    presentations: list[AdminPresentation]


class AdminPerfumeSummary(BaseModel):
    """One row of the admin perfume list."""

    id: UUID
    slug: str
    name: str
    brand: PerfumeBrandRef
    concentration: PerfumeConcentrationRef
    gender: Literal["women", "men", "unisex"]
    is_published: bool
    is_archived: bool
    active_presentations: int
    created_at: datetime


class AdminPerfumePage(Page[AdminPerfumeSummary]):
    """One page of the admin perfume list."""
