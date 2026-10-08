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
