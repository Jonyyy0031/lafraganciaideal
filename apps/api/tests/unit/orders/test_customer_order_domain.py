"""Plan 005 step 1, domain: the customer's cancel and lookup rules. `cancelled_by` for each kind
of actor (customer, system, anything else is the shop), the constants of decisions 39 and 41, and
the two lookup errors (code, message, kind)."""

from datetime import timedelta

import pytest

from fragancia_api.modules.orders.domain.errors import (
    OrderLookupNotFound,
    OrderLookupRateLimited,
)
from fragancia_api.modules.orders.domain.order import (
    CUSTOMER_ACTOR,
    CUSTOMER_CANCEL_REASON,
    LOOKUP_IP_MAX,
    LOOKUP_NUMBER_MAX,
    LOOKUP_WINDOW,
    SYSTEM_ACTOR,
    cancelled_by,
)
from fragancia_api.shared.kernel import NotFoundError, RateLimitedError


def test_the_customer_actor_cancels_as_the_customer() -> None:
    assert cancelled_by(CUSTOMER_ACTOR) == "customer"


def test_the_system_actor_cancels_as_the_system() -> None:
    assert cancelled_by(SYSTEM_ACTOR) == "system"


@pytest.mark.parametrize("actor", ["owner-1", "admin-1", "", "Customer", "SYSTEM", "customer "])
def test_any_other_actor_is_the_shop(actor: str) -> None:
    assert cancelled_by(actor) == "shop"


def test_the_actor_and_reason_constants_are_the_planned_ones() -> None:
    assert CUSTOMER_ACTOR == "customer"
    assert SYSTEM_ACTOR == "system"
    assert CUSTOMER_CANCEL_REASON == "Cancelado por el cliente"


def test_the_lookup_limits_are_the_planned_ones() -> None:
    assert LOOKUP_WINDOW == timedelta(minutes=15)
    assert LOOKUP_IP_MAX == 20
    assert LOOKUP_NUMBER_MAX == 5


def test_the_lookup_not_found_is_a_404_kind_with_its_code_and_message() -> None:
    error = OrderLookupNotFound()

    assert isinstance(error, NotFoundError)
    assert error.code == "ORDERS_LOOKUP_NOT_FOUND"
    assert error.message == "No encontramos un pedido con ese número y correo"


def test_the_lookup_rate_limited_is_a_429_kind_with_its_code_and_message() -> None:
    error = OrderLookupRateLimited()

    assert isinstance(error, RateLimitedError)
    assert error.code == "ORDERS_LOOKUP_RATE_LIMITED"
    assert error.message == "Demasiados intentos; espera unos minutos"
