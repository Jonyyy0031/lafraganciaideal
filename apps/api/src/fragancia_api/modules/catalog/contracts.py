"""HTTP contracts of the catalog (source of truth for OpenAPI and the generated web client)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

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
