"""`Checkout.reusable` against the order's deadline (checkout.py) and the gateway error's 503."""

from datetime import timedelta

import pytest

from fragancia_api.modules.payments.domain import checkout as checkout_module
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


def test_there_is_no_reuse_margin() -> None:
    assert not hasattr(checkout_module, "REUSE_MARGIN")


@pytest.mark.parametrize("offset", [timedelta(minutes=30), SECOND, timedelta(milliseconds=1)])
def test_a_checkout_with_the_orders_deadline_is_reusable_until_it_expires(
    offset: timedelta,
) -> None:
    due = NOW + offset
    assert make_checkout(due).reusable(due, NOW) is True


def test_a_checkout_in_its_last_minute_is_still_reusable() -> None:
    due = NOW + SECOND
    assert make_checkout(due).reusable(due, NOW) is True


@pytest.mark.parametrize("offset", [timedelta(0), -SECOND, -timedelta(hours=1)])
def test_an_expired_checkout_is_not_reusable(offset: timedelta) -> None:
    due = NOW + offset
    assert make_checkout(due).reusable(due, NOW) is False


@pytest.mark.parametrize("shift", [SECOND, -SECOND, timedelta(minutes=30)])
def test_a_checkout_for_another_deadline_is_not_reusable(shift: timedelta) -> None:
    due = NOW + timedelta(minutes=30)
    assert make_checkout(due + shift).reusable(due, NOW) is False


def test_the_not_found_message_is_in_spanish() -> None:
    assert PaymentOrderNotFound().message == "No encontramos ese pedido"


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
