"""Plan 003, wiring: the real container resolves the pending-tickets query, subscribes the close of
the Mercado Pago link to payments' own event with the gateway the checkout uses, and
`OrderPaymentsAdapter.hold` maps the orders facade's outcomes to payments' words."""

from datetime import timedelta
from typing import Any
from uuid import UUID

from fragancia_api.container import build_container
from fragancia_api.modules.orders.application.commands.confirm_payment import (
    OrderPaymentConfirmations,
)
from fragancia_api.modules.orders.domain.order import OrderStatus
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryStockHolds
from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.application.handlers.cancel_ticket import CancelTicket
from fragancia_api.modules.payments.application.handlers.close_checkout import CloseCheckout
from fragancia_api.modules.payments.application.ports import Confirmation
from fragancia_api.modules.payments.application.queries.pending_tickets import GetPendingTickets
from fragancia_api.modules.payments.domain.events import (
    CheckoutCloseRequested,
    TicketCancelRequested,
)
from fragancia_api.modules.payments.infrastructure.order_payments import OrderPaymentsAdapter
from fragancia_api.modules.payments.infrastructure.sql_checkout_repository import (
    SqlCheckoutRepository,
)
from fragancia_api.modules.payments.infrastructure.sql_payment_repository import SqlPaymentQueries
from fragancia_api.shared.kernel import ConflictError, Err, Ok
from tests.support import make_settings
from tests.unit.orders.test_order_lifecycle import World as OrdersWorld


async def test_the_container_resolves_the_pending_tickets_query_over_sql() -> None:
    container = build_container(make_settings())
    try:
        query: Any = container.services.get(GetPendingTickets)

        assert isinstance(query, GetPendingTickets)
        assert isinstance(query._queries, SqlPaymentQueries)  # noqa: SLF001
        assert isinstance(query._orders, OrderPaymentsAdapter)  # noqa: SLF001
    finally:
        await container.close()


async def test_cancel_ticket_alone_handles_the_cancel_request_with_the_shared_gateway() -> None:
    container = build_container(make_settings(mercadopago_access_token="TEST-token"))
    try:
        handlers = container.relay._bus.handlers(TicketCancelRequested.name)  # noqa: SLF001
        assert TicketCancelRequested.name == "payments.ticket.cancel_requested"
        assert [type(h) for h in handlers] == [CancelTicket]
        gateway = container.services.get(StartCheckout)._gateway  # noqa: SLF001
        assert handlers[0]._gateway is gateway  # type: ignore[attr-defined]  # noqa: SLF001
    finally:
        await container.close()


async def test_the_close_of_the_link_listens_to_its_own_event_with_the_checkouts_gateway() -> None:
    container = build_container(make_settings(mercadopago_access_token="TEST-token"))
    try:
        [closer] = container.relay._bus.handlers(CheckoutCloseRequested.name)  # noqa: SLF001
        assert isinstance(closer, CloseCheckout)
        gateway = container.services.get(StartCheckout)._gateway  # noqa: SLF001
        assert closer._gateway is gateway  # noqa: SLF001
        assert isinstance(closer._checkouts, SqlCheckoutRepository)  # noqa: SLF001
    finally:
        await container.close()


async def test_the_order_adapter_maps_the_hold_outcomes_to_payments_words() -> None:
    world = OrdersWorld()
    await world.store()
    adapter = OrderPaymentsAdapter(None, world.wire(OrderPaymentConfirmations))  # type: ignore[arg-type]
    until = world.current().payment_due_at + timedelta(hours=40)

    held = await adapter.hold(UUID(int=901), until, "efectivo en tienda", "https://mp.test/t")
    missing = await adapter.hold(UUID(int=4242), until, "efectivo en tienda", None)
    await world.store(n=2)
    world.current(2).status = OrderStatus.PAID
    not_pending = await adapter.hold(UUID(int=902), until, "transferencia SPEI", None)

    assert held == Ok(Confirmation(outcome="held", number=1001))
    assert missing == Ok(Confirmation(outcome="not_found", number=None))
    assert not_pending == Ok(Confirmation(outcome="not_pending", number=1002))


async def test_the_order_adapter_answers_held_even_when_inventory_refuses_the_extension() -> None:
    # `hold_for_payment` never returns inventory's refusal: it logs it and keeps the deadline.
    world = OrdersWorld(
        stocks=InMemoryStockHolds(
            extend_refusal=ConflictError(code="INVENTORY_RESERVATION_EXPIRED", message="x")
        )
    )
    await world.store()
    adapter = OrderPaymentsAdapter(None, world.wire(OrderPaymentConfirmations))  # type: ignore[arg-type]
    until = world.current().payment_due_at + timedelta(hours=40)

    result = await adapter.hold(UUID(int=901), until, "efectivo en tienda", None)

    assert not isinstance(result, Err)
    assert result == Ok(Confirmation(outcome="held", number=1001))
