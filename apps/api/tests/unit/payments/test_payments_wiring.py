"""The real container resolves the payments service, and its gateway follows the settings."""

from typing import Any
from uuid import UUID

import pytest

from fragancia_api.container import MODULES, build_container
from fragancia_api.modules.identity import OwnerDirectory
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.in_memory import InMemoryUsers
from fragancia_api.modules.orders.application.commands.confirm_payment import (
    OrderPaymentConfirmations,
)
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryStockHolds
from fragancia_api.modules.payments.application.commands.receive_notification import (
    ReceivePaymentNotification,
)
from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.application.handlers.flagged_payment_email import (
    SendFlaggedPaymentToOwners,
)
from fragancia_api.modules.payments.application.handlers.process_payment import (
    ProcessPaymentNotification,
)
from fragancia_api.modules.payments.application.ports import Confirmation, OwnerContact
from fragancia_api.modules.payments.domain.events import (
    PaymentFlagged,
    PaymentNotificationReceived,
)
from fragancia_api.modules.payments.infrastructure.identity_owners import IdentityOwners
from fragancia_api.modules.payments.infrastructure.mercadopago_gateway import MercadoPagoGateway
from fragancia_api.modules.payments.infrastructure.order_payments import OrderPaymentsAdapter
from fragancia_api.modules.payments.infrastructure.sql_payment_repository import (
    SqlPaymentRepository,
)
from fragancia_api.modules.payments.module import module as payments_module
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import ConflictError, Err, Ok
from tests.support import make_settings
from tests.unit.orders.test_order_lifecycle import World as OrdersWorld

NOW = FixedClock().now()


def test_payments_is_registered_after_orders() -> None:
    names = [m.name for m in MODULES]

    assert "payments" in names
    assert names.index("payments") > names.index("orders")
    assert payments_module in MODULES


async def test_the_container_resolves_start_checkout() -> None:
    container = build_container(make_settings())

    assert isinstance(container.services.get(StartCheckout), StartCheckout)
    await container.close()


async def test_the_gateway_takes_its_settings_and_the_back_url_the_storefront() -> None:
    container = build_container(
        make_settings(
            mercadopago_access_token="TEST-token",
            mercadopago_api_url="http://127.0.0.1:9/",
            mercadopago_sandbox=False,
            storefront_url="https://shop.test",
        )
    )

    use_case = container.services.get(StartCheckout)
    gateway = use_case._gateway  # noqa: SLF001
    assert isinstance(gateway, MercadoPagoGateway)
    assert gateway._api_url == "http://127.0.0.1:9"  # noqa: SLF001
    assert gateway._access_token == "TEST-token"  # noqa: SLF001
    assert gateway._sandbox is False  # noqa: SLF001
    assert use_case._back_url == "https://shop.test/pedido/pago"  # noqa: SLF001
    await container.close()


async def test_the_container_resolves_the_webhook_command_with_the_secret() -> None:
    container = build_container(make_settings(mercadopago_webhook_secret="whsec-test"))

    use_case = container.services.get(ReceivePaymentNotification)
    assert isinstance(use_case, ReceivePaymentNotification)
    assert use_case._secret == "whsec-test"  # noqa: SLF001
    await container.close()


async def test_without_a_webhook_secret_the_container_builds_with_no_secret() -> None:
    container = build_container(make_settings())

    use_case = container.services.get(ReceivePaymentNotification)
    assert use_case._secret is None  # noqa: SLF001
    await container.close()


async def test_the_container_subscribes_the_two_payment_handlers_to_their_events() -> None:
    container = build_container(make_settings())
    try:
        bus = container.relay._bus  # noqa: SLF001
        received = bus.handlers(PaymentNotificationReceived.name)
        flagged = bus.handlers(PaymentFlagged.name)

        assert [type(h) for h in received] == [ProcessPaymentNotification]
        assert [type(h) for h in flagged] == [SendFlaggedPaymentToOwners]
        process: Any = received[0]
        assert isinstance(process._gateway, MercadoPagoGateway)  # noqa: SLF001
        assert isinstance(process._payments, SqlPaymentRepository)  # noqa: SLF001
        assert isinstance(process._orders, OrderPaymentsAdapter)  # noqa: SLF001
        email: Any = flagged[0]
        assert isinstance(email._owners, IdentityOwners)  # noqa: SLF001
    finally:
        await container.close()


async def test_the_gateway_of_the_subscriber_is_the_one_the_checkout_uses() -> None:
    container = build_container(make_settings(mercadopago_access_token="TEST-token"))
    try:
        [process] = container.relay._bus.handlers(PaymentNotificationReceived.name)  # noqa: SLF001
        gateway = container.services.get(StartCheckout)._gateway  # noqa: SLF001
        assert process._gateway is gateway  # type: ignore[attr-defined]  # noqa: SLF001
    finally:
        await container.close()


async def test_the_owner_adapter_maps_only_active_owners_to_payments_contacts() -> None:
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

    assert await owners.active_owners() == [OwnerContact(name="Ana", email="ana@example.test")]


async def test_the_order_adapter_maps_the_facade_outcome_to_payments_words() -> None:
    world = OrdersWorld()
    await world.store()
    confirmations = world.wire(OrderPaymentConfirmations)
    adapter = OrderPaymentsAdapter(None, confirmations)  # type: ignore[arg-type]

    paid = await adapter.confirm(UUID(int=901), "1", world.current().total_cents, "MXN")
    again = await adapter.confirm(UUID(int=901), "1", world.current().total_cents, "MXN")
    missing = await adapter.confirm(UUID(int=4242), "1", 1, "MXN")

    assert paid == Ok(Confirmation(outcome="paid", number=1001))
    assert again == Ok(Confirmation(outcome="not_pending", number=1001))
    assert missing == Ok(Confirmation(outcome="not_found", number=None))


async def test_the_order_adapter_passes_a_refusal_through() -> None:
    world = OrdersWorld(
        stocks=InMemoryStockHolds(
            refusal=ConflictError(code="INVENTORY_RESERVATION_COMMITTED", message="x")
        )
    )
    await world.store()
    adapter = OrderPaymentsAdapter(None, world.wire(OrderPaymentConfirmations))  # type: ignore[arg-type]

    result = await adapter.confirm(UUID(int=901), "1", world.current().total_cents, "MXN")

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_RESERVATION_COMMITTED"


@pytest.mark.parametrize("token", [None])
async def test_without_a_token_the_container_still_builds_outside_production(
    token: str | None,
) -> None:
    container = build_container(make_settings(mercadopago_access_token=token))

    gateway = container.services.get(StartCheckout)._gateway  # noqa: SLF001
    assert isinstance(gateway, MercadoPagoGateway)
    assert gateway._access_token is None  # noqa: SLF001
    await container.close()
