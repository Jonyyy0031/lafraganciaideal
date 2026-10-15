from dataclasses import dataclass

from fragancia_api.shared.kernel import InvalidValueError


@dataclass(frozen=True)
class DeliveryOptionsInvalid(InvalidValueError):
    """`details`: `{"field": <name>, …}`."""

    code: str = "ORDERS_DELIVERY_OPTIONS_INVALID"
    message: str = "The delivery options break a rule"


@dataclass(frozen=True)
class PostalCodeInvalid(InvalidValueError):
    code: str = "ORDERS_POSTAL_CODE_INVALID"
    message: str = "A postal code is exactly 5 digits"
