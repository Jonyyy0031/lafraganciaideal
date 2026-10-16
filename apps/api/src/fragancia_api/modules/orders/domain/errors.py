from dataclasses import dataclass

from fragancia_api.shared.kernel import ConflictError, InvalidValueError


@dataclass(frozen=True)
class DeliveryOptionsInvalid(InvalidValueError):
    """`details`: `{"field": <name>, …}`."""

    code: str = "ORDERS_DELIVERY_OPTIONS_INVALID"
    message: str = "The delivery options break a rule"


@dataclass(frozen=True)
class PostalCodeInvalid(InvalidValueError):
    code: str = "ORDERS_POSTAL_CODE_INVALID"
    message: str = "A postal code is exactly 5 digits"


@dataclass(frozen=True)
class CartQuantityInvalid(InvalidValueError):
    """`details`: `{"min": 1, "max": 10}`."""

    code: str = "ORDERS_CART_QUANTITY_INVALID"
    message: str = "La cantidad debe estar entre 1 y 10"


@dataclass(frozen=True)
class CartTooManyLines(InvalidValueError):
    """`details`: `{"max": <lines>}`."""

    code: str = "ORDERS_CART_TOO_MANY_LINES"
    message: str = "El carrito tiene demasiados productos"


@dataclass(frozen=True)
class CartPresentationUnavailable(ConflictError):
    code: str = "ORDERS_CART_PRESENTATION_UNAVAILABLE"
    message: str = "Este producto ya no está disponible"


@dataclass(frozen=True)
class CartStockUnavailable(ConflictError):
    """`details`: `{"available": <units>}` (decision 21)."""

    code: str = "ORDERS_CART_STOCK_UNAVAILABLE"
    message: str = "No hay suficientes unidades disponibles"
