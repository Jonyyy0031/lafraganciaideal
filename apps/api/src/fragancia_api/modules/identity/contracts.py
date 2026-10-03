"""HTTP contracts of identity (source of truth for OpenAPI and the generated web client)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # Business rules (email shape, password length) live in the domain; this only bounds the
    # payload.
    email: str = Field(max_length=320, examples=["owner@example.test"])
    password: str = Field(max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class AdminMe(BaseModel):
    """The signed-in back-office user."""

    id: UUID
    email: str
    name: str
    role: Literal["owner", "staff"]
    permissions: list[str] = Field(description="Sorted permission names")


class LoginResponse(BaseModel):
    """The signed-in user and when the session expires at the latest. The session token
    travels only in the httpOnly cookie."""

    user: AdminMe
    expires_at: datetime


class AdminSession(BaseModel):
    """One of my open sessions."""

    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip: str | None
    current: bool
