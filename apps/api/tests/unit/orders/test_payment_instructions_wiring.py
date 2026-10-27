"""Payments plan 003 step 3, wiring: the real container subscribes the instructions email to
`orders.order.payment_pending` with the settings' storefront URL and the real adapters."""

from typing import Any

from fragancia_api.container import build_container
from fragancia_api.modules.orders.application.handlers.order_emails import (
    SendPaymentInstructionsEmail,
)
from fragancia_api.modules.orders.domain.order import OrderPaymentPending
from fragancia_api.modules.orders.infrastructure.order_links import HmacOrderLinks
from fragancia_api.modules.orders.infrastructure.sql_order_queries import SqlOrderQueries
from tests.support import make_settings

STOREFRONT = "https://tienda.example.test"


async def test_the_container_subscribes_only_the_instructions_email_to_payment_pending() -> None:
    container = build_container(make_settings(storefront_url=STOREFRONT))
    try:
        handlers = container.relay._bus.handlers(OrderPaymentPending.name)  # noqa: SLF001

        assert OrderPaymentPending.name == "orders.order.payment_pending"
        assert [type(h) for h in handlers] == [SendPaymentInstructionsEmail]
        handler: Any = handlers[0]
        assert handler._storefront_url == STOREFRONT  # noqa: SLF001
        assert isinstance(handler._links, HmacOrderLinks)  # noqa: SLF001
        assert isinstance(handler._queries, SqlOrderQueries)  # noqa: SLF001
    finally:
        await container.close()
