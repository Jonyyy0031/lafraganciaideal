"""Expected errors. The HTTP layer maps each CATEGORY to a status code; `code` is stable and
is what clients use for translations (e.g. `CATALOG_BRAND_ALREADY_EXISTS`).

Concrete errors subclass a category and give `code` and `message` defaults:

    @dataclass(frozen=True)
    class BrandAlreadyExists(ConflictError):
        code: str = "CATALOG_BRAND_ALREADY_EXISTS"
        message: str = "A brand with this name already exists"
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class DomainError:
    code: str
    message: str
    details: Mapping[str, object] | None = None


@dataclass(frozen=True)
class NotFoundError(DomainError):
    """The referenced resource does not exist (HTTP 404)."""


@dataclass(frozen=True)
class ConflictError(DomainError):
    """The operation clashes with the current state, e.g. a duplicate (HTTP 409)."""


@dataclass(frozen=True)
class InvalidValueError(DomainError):
    """A value breaks its own rules, e.g. a name that is too short (HTTP 422)."""


@dataclass(frozen=True)
class BusinessRuleViolationError(DomainError):
    """A valid request that a business rule forbids, e.g. shipping an unpaid order (HTTP 422)."""


@dataclass(frozen=True)
class UnauthenticatedError(DomainError):
    """The caller could not be identified, e.g. wrong credentials (HTTP 401)."""


@dataclass(frozen=True)
class RateLimitedError(DomainError):
    """Too many attempts; try again later (HTTP 429)."""
