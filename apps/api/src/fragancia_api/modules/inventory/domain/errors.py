from dataclasses import dataclass

from fragancia_api.shared.kernel import (
    BusinessRuleViolationError,
    ConflictError,
    InvalidValueError,
    NotFoundError,
)


@dataclass(frozen=True)
class PresentationNotFound(NotFoundError):
    code: str = "INVENTORY_PRESENTATION_NOT_FOUND"
    message: str = "Presentation not found"


@dataclass(frozen=True)
class PresentationNotStocked(BusinessRuleViolationError):
    code: str = "INVENTORY_PRESENTATION_NOT_STOCKED"
    message: str = "Only presentations sold in stock have a unit count"


@dataclass(frozen=True)
class StockQuantityInvalid(InvalidValueError):
    code: str = "INVENTORY_QUANTITY_INVALID"
    message: str = "The quantity is outside the range this kind of movement accepts"


@dataclass(frozen=True)
class StockReasonInvalid(InvalidValueError):
    code: str = "INVENTORY_REASON_INVALID"
    message: str = "A reason needs 1 to 200 characters"


@dataclass(frozen=True)
class ThresholdInvalid(InvalidValueError):
    code: str = "INVENTORY_THRESHOLD_INVALID"
    message: str = "A low-stock threshold is a whole number from 0 to 1000"


@dataclass(frozen=True)
class StockInsufficient(ConflictError):
    code: str = "INVENTORY_STOCK_INSUFFICIENT"
    message: str = "There are not enough units on hand for this movement"
