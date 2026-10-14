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


@dataclass(frozen=True)
class StockUnavailable(ConflictError):
    """`details`, set by the command: `{"lines": [{"presentation_id", "requested",
    "available"}]}`, one entry per short presentation."""

    code: str = "INVENTORY_STOCK_UNAVAILABLE"
    message: str = "Not enough units are available"


@dataclass(frozen=True)
class ReservationNotFound(NotFoundError):
    code: str = "INVENTORY_RESERVATION_NOT_FOUND"
    message: str = "Reservation not found"


@dataclass(frozen=True)
class ReservationReferenceTaken(ConflictError):
    code: str = "INVENTORY_RESERVATION_REFERENCE_TAKEN"
    message: str = "A reservation with this reference already exists"


@dataclass(frozen=True)
class ReservationInvalid(InvalidValueError):
    code: str = "INVENTORY_RESERVATION_INVALID"
    message: str = (
        "A reservation needs a reference of 1 to 64 characters and at least one line, each "
        "with a quantity from 1 to 100000"
    )


@dataclass(frozen=True)
class ReservationExpiryInvalid(InvalidValueError):
    code: str = "INVENTORY_RESERVATION_EXPIRY_INVALID"
    message: str = (
        "The expiry must be in the future and, when extending, later than the current one"
    )


@dataclass(frozen=True)
class ReservationNotActive(ConflictError):
    code: str = "INVENTORY_RESERVATION_NOT_ACTIVE"
    message: str = "The reservation is not active or is past its expiry"


@dataclass(frozen=True)
class ReservationCommitted(ConflictError):
    code: str = "INVENTORY_RESERVATION_COMMITTED"
    message: str = "The reservation was already committed"
