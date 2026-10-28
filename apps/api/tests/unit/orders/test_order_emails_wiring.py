"""Plan 006, wiring: the real container subscribes the three email handlers to the three event names
orders publishes, with the settings' URLs and the real order-link adapter."""

from typing import Any

from fragancia_api.container import build_container
from fragancia_api.modules.identity import OwnerDirectory
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.in_memory import InMemoryUsers
from fragancia_api.modules.orders.application.handlers.order_emails import (
    SendOrderPlacedEmail,
    SendOrderStatusEmail,
    SendPaidOrderToOwners,
)
from fragancia_api.modules.orders.application.ports import Recipient
from fragancia_api.modules.orders.domain.order import OrderPaid, OrderPlaced, OrderStatusChanged
from fragancia_api.modules.orders.infrastructure.identity_owners import IdentityOwners
from fragancia_api.modules.orders.infrastructure.order_links import HmacOrderLinks
from fragancia_api.modules.orders.infrastructure.sql_order_queries import SqlOrderQueries
from fragancia_api.modules.payments.application.handlers.close_checkout import (
    RequestCheckoutClose,
)
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from tests.support import make_settings

NOW = FixedClock().now()
STOREFRONT = "https://tienda.example.test"
ADMIN_WEB = "https://admin.example.test"


async def test_the_container_subscribes_the_three_email_handlers() -> None:
    container = build_container(make_settings(storefront_url=STOREFRONT, admin_web_url=ADMIN_WEB))
    try:
        bus = container.relay._bus  # noqa: SLF001
        placed = bus.handlers(OrderPlaced.name)
        changed = bus.handlers(OrderStatusChanged.name)

        assert [type(h) for h in placed] == [SendOrderPlacedEmail]
        paid = bus.handlers(OrderPaid.name)

        # Payments (plan 003 of payments-mercadopago) also listens to the status changes.
        assert [type(h) for h in changed] == [SendOrderStatusEmail, RequestCheckoutClose]
        assert [type(h) for h in paid] == [SendPaidOrderToOwners]
    finally:
        await container.close()


async def test_the_handlers_get_the_configured_urls_and_the_real_adapters() -> None:
    container = build_container(make_settings(storefront_url=STOREFRONT, admin_web_url=ADMIN_WEB))
    try:
        bus = container.relay._bus  # noqa: SLF001
        placed: Any
        status: Any
        owners: Any
        [placed] = bus.handlers(OrderPlaced.name)
        [status, _request_close] = bus.handlers(OrderStatusChanged.name)
        [owners] = bus.handlers(OrderPaid.name)

        for customer_handler in (placed, status):
            assert customer_handler._storefront_url == STOREFRONT  # noqa: SLF001
            assert isinstance(customer_handler._links, HmacOrderLinks)  # noqa: SLF001
            assert isinstance(customer_handler._queries, SqlOrderQueries)  # noqa: SLF001
        assert owners._admin_web_url == ADMIN_WEB  # noqa: SLF001
        assert isinstance(owners._owners, IdentityOwners)  # noqa: SLF001
    finally:
        await container.close()


async def test_the_owner_adapter_maps_only_active_owners_to_orders_recipients() -> None:
    users = InMemoryUsers()
    for address, name, role, active in [
        ("ana@example.test", "Ana", Role.OWNER, True),
        ("gone@example.test", "Gone", Role.OWNER, False),
        ("staff@example.test", "Staff", Role.STAFF, True),
    ]:
        user = User.create(Email(address), DisplayName(name), role, "hash", now=NOW)
        if not active:
            user.deactivate()
        await users.save(user)

    owners = IdentityOwners(OwnerDirectory(users))

    assert await owners.active_owners() == [Recipient(name="Ana", email="ana@example.test")]
