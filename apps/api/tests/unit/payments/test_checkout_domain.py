"""`Checkout.reusable` at the margin (checkout.py) and the 503 category of the gateway error."""

from datetime import timedelta

import pytest

from fragancia_api.modules.payments.domain.checkout import REUSE_MARGIN
from fragancia_api.modules.payments.domain.errors import (
    PaymentGatewayUnavailable,
    PaymentOrderNotFound,
    PaymentOrderNotPayable,
)
from fragancia_api.shared.http.errors import _STATUS_BY_CATEGORY
from fragancia_api.shared.kernel import (
    ConflictError,
    DomainError,
    NotFoundError,
    ServiceUnavailableError,
)
from tests.unit.payments.support import NOW, make_checkout

SECOND = timedelta(seconds=1)


def test_the_reuse_margin_is_two_minutes() -> None:
    assert REUSE_MARGIN == timedelta(minutes=2)


def test_a_checkout_far_from_expiry_is_reusable() -> None:
    assert make_checkout(NOW + timedelta(minutes=30)).reusable(NOW) is True


def test_a_checkout_just_outside_the_margin_is_reusable() -> None:
    assert make_checkout(NOW + REUSE_MARGIN + SECOND).reusable(NOW) is True


def test_a_checkout_exactly_at_the_margin_is_not_reusable() -> None:
    assert make_checkout(NOW + REUSE_MARGIN).reusable(NOW) is False


def test_a_checkout_inside_the_margin_is_not_reusable() -> None:
    assert make_checkout(NOW + REUSE_MARGIN - SECOND).reusable(NOW) is False


@pytest.mark.parametrize("offset", [timedelta(0), -SECOND, -timedelta(hours=1)])
def test_an_expired_checkout_is_not_reusable(offset: timedelta) -> None:
    assert make_checkout(NOW + offset).reusable(NOW) is False


def test_the_gateway_error_is_a_service_unavailable_error_with_its_code() -> None:
    error = PaymentGatewayUnavailable()

    assert isinstance(error, ServiceUnavailableError)
    assert error.code == "PAYMENTS_GATEWAY_UNAVAILABLE"
    assert error.message == "No pudimos conectar con Mercado Pago; intenta de nuevo"


def _status_of(error: DomainError) -> int:
    return next(status for category, status in _STATUS_BY_CATEGORY if isinstance(error, category))


def test_the_unavailable_category_maps_to_503() -> None:
    assert _status_of(PaymentGatewayUnavailable()) == 503
    assert _status_of(ServiceUnavailableError(code="X", message="x")) == 503


def test_the_other_payment_errors_keep_their_categories() -> None:
    assert isinstance(PaymentOrderNotFound(), NotFoundError)
    assert PaymentOrderNotFound().code == "PAYMENTS_ORDER_NOT_FOUND"
    assert isinstance(PaymentOrderNotPayable(), ConflictError)
    assert PaymentOrderNotPayable().code == "PAYMENTS_ORDER_NOT_PAYABLE"
    assert PaymentOrderNotPayable().message == "Este pedido ya no se puede pagar"
