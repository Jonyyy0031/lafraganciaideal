from dataclasses import dataclass

from fragancia_api.shared.kernel import (
    ConflictError,
    InvalidValueError,
    NotFoundError,
    RateLimitedError,
)


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


@dataclass(frozen=True)
class CustomerDataInvalid(InvalidValueError):
    """`details`: `{"field": "<path>"}`, e.g. `contact.phone` or `address.state`."""

    code: str = "ORDERS_CUSTOMER_DATA_INVALID"
    message: str = "Revisa los datos del pedido"


@dataclass(frozen=True)
class CheckoutCartEmpty(ConflictError):
    code: str = "ORDERS_CHECKOUT_CART_EMPTY"
    message: str = "Tu carrito está vacío"


@dataclass(frozen=True)
class CheckoutCartHasProblems(ConflictError):
    """`details`: `{"lines": [{"presentation_id", "status"}]}` for every line that is not ok."""

    code: str = "ORDERS_CHECKOUT_CART_HAS_PROBLEMS"
    message: str = "Revisa tu carrito: hay productos agotados o no disponibles"


@dataclass(frozen=True)
class CheckoutCartChanged(ConflictError):
    code: str = "ORDERS_CHECKOUT_CART_CHANGED"
    message: str = "Tu carrito cambió; revísalo e intenta de nuevo"


@dataclass(frozen=True)
class CheckoutDeliveryUnavailable(ConflictError):
    """`details`: `{"method"}`."""

    code: str = "ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE"
    message: str = "Ese método de entrega no está disponible para tu pedido"


@dataclass(frozen=True)
class CheckoutTotalChanged(ConflictError):
    """`details`: `{"subtotal_cents", "delivery_cents", "total_cents"}` (decision 26)."""

    code: str = "ORDERS_CHECKOUT_TOTAL_CHANGED"
    message: str = "El total de tu pedido cambió"


@dataclass(frozen=True)
class CheckoutStockUnavailable(ConflictError):
    """`details`: inventory's `{"lines": [{"presentation_id", "requested", "available"}]}`."""

    code: str = "ORDERS_CHECKOUT_STOCK_UNAVAILABLE"
    message: str = "Ya no hay suficientes unidades de algunos productos"


@dataclass(frozen=True)
class OrderNotFound(NotFoundError):
    code: str = "ORDERS_ORDER_NOT_FOUND"
    message: str = "No encontramos ese pedido"


@dataclass(frozen=True)
class OrderTransitionInvalid(ConflictError):
    """`details`: `{"from": <status>, "to": <status>}`."""

    code: str = "ORDERS_TRANSITION_INVALID"
    message: str = "El pedido no puede pasar a ese estado"


@dataclass(frozen=True)
class OrderDataInvalid(InvalidValueError):
    """`details`: `{"field": "note" | "reason" | "carrier" | "tracking_number"}`."""

    code: str = "ORDERS_ORDER_DATA_INVALID"
    message: str = "Revisa los datos del cambio"


@dataclass(frozen=True)
class OrderLookupNotFound(NotFoundError):
    """The same for an unknown number and for a wrong email (decision 39)."""

    code: str = "ORDERS_LOOKUP_NOT_FOUND"
    message: str = "No encontramos un pedido con ese número y correo"


@dataclass(frozen=True)
class OrderLookupRateLimited(RateLimitedError):
    code: str = "ORDERS_LOOKUP_RATE_LIMITED"
    message: str = "Demasiados intentos; espera unos minutos"
