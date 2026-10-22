from dataclasses import dataclass

from fragancia_api.shared.kernel import ConflictError, NotFoundError, ServiceUnavailableError


@dataclass(frozen=True)
class PaymentOrderNotFound(NotFoundError):
    code: str = "PAYMENTS_ORDER_NOT_FOUND"
    message: str = "Order not found"


@dataclass(frozen=True)
class PaymentOrderNotPayable(ConflictError):
    """`details`: `{"status"}`."""

    code: str = "PAYMENTS_ORDER_NOT_PAYABLE"
    message: str = "Este pedido ya no se puede pagar"


@dataclass(frozen=True)
class PaymentGatewayUnavailable(ServiceUnavailableError):
    code: str = "PAYMENTS_GATEWAY_UNAVAILABLE"
    message: str = "No pudimos conectar con Mercado Pago; intenta de nuevo"
