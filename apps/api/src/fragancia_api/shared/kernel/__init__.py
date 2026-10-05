"""Shared kernel: pure building blocks for every domain. Standard library only, no IO."""

from fragancia_api.shared.kernel.entity import AggregateRoot
from fragancia_api.shared.kernel.errors import (
    BusinessRuleViolationError,
    ConflictError,
    DomainError,
    InvalidValueError,
    NotFoundError,
    RateLimitedError,
    UnauthenticatedError,
)
from fragancia_api.shared.kernel.events import DomainEvent
from fragancia_api.shared.kernel.ids import new_id
from fragancia_api.shared.kernel.money import Money
from fragancia_api.shared.kernel.result import Err, Ok, Result

__all__ = [
    "AggregateRoot",
    "BusinessRuleViolationError",
    "ConflictError",
    "DomainError",
    "DomainEvent",
    "Err",
    "InvalidValueError",
    "Money",
    "NotFoundError",
    "Ok",
    "RateLimitedError",
    "Result",
    "UnauthenticatedError",
    "new_id",
]
