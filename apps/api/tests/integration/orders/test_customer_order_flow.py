"""Plan 005 end to end on PostgreSQL (`fragancia_test`): the customer commands over the real orders
adapters and the real inventory. The private-link token of an order placed through the real
checkout opens the view; the lookup answers that same token; a wrong email and an unknown number
answer equal errors; the attempts are really COMMITTED when the sixth is refused (rows in
`orders.lookup_throttle`); the customer cancel releases the real reservation; and `cancelled_by`
tells customer, system and shop apart without ever showing the reason."""

import pytest

from fragancia_api.container import Container
from fragancia_api.modules.orders.application.commands.customer_order import (
    CancelOrderByCustomer,
    LookUpOrder,
)
from fragancia_api.modules.orders.application.commands.lifecycle import (
    CancelOverdueOrders,
    CancelPaidOrder,
    CancelUnpaidOrder,
    MarkOrderRefunded,
)
from fragancia_api.modules.orders.application.queries.customer_order import GetCustomerOrder
from fragancia_api.modules.orders.domain.errors import (
    OrderLookupNotFound,
    OrderLookupRateLimited,
    OrderNotFound,
    OrderTransitionInvalid,
)
from fragancia_api.modules.orders.domain.order import LOOKUP_NUMBER_MAX
from fragancia_api.modules.orders.infrastructure.order_links import HmacOrderLinks
from fragancia_api.modules.orders.infrastructure.sql_customer_orders import SqlCustomerOrders
from fragancia_api.modules.orders.infrastructure.sql_lookup_throttle import SqlLookupThrottle
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import item_row, reservation_row
from tests.integration.orders.test_checkout_flow import rows
from tests.integration.orders.test_order_lifecycle_flow import (
    OWNER,
    history,
    ok,
    order_row,
    pay,
    place,
    refused,
)
from tests.unit.orders.test_customer_order import (
    ADDRESS_KEYS,
    FORBIDDEN,
    LINE_KEYS,
    STEP_KEYS,
    VIEW_KEYS,
)

pytestmark = pytest.mark.integration

# The secret the integration checkout signs its private links with.
LINKS = HmacOrderLinks("integration-order-link-secret-0123456789")
EMAIL = "ana@example.com"  # what `tests.unit.orders.test_checkout.contact` places orders with


def view_query(container: Container) -> GetCustomerOrder:
    return GetCustomerOrder(queries=SqlCustomerOrders(container.database), links=LINKS)


def lookup_command(container: Container) -> LookUpOrder:
    return LookUpOrder(
        queries=SqlCustomerOrders(container.database),
        links=LINKS,
        throttle=SqlLookupThrottle(container.database),
        transactions=SqlTransactionRunner(container.database),
        clock=SystemClock(),
    )


def cancel_command(container: Container) -> CancelOrderByCustomer:
    return CancelOrderByCustomer(
        queries=SqlCustomerOrders(container.database),
        links=LINKS,
        cancel=container.services.get(CancelUnpaidOrder),
    )


async def token_of(container: Container, number: int) -> str:
    return LINKS.token((await order_row(container, number))["id"])


async def customer_email(container: Container, number: int) -> str:
    return str((await order_row(container, number))["customer_email"])


async def view(container: Container, number: int):  # type: ignore[no-untyped-def]
    result = await view_query(container).execute(await token_of(container, number))
    assert isinstance(result, Ok), result
    return result.value


async def throttle_rows(container: Container) -> dict[str, int]:
    found = await rows(container, "SELECT key, attempts FROM orders.lookup_throttle")
    return {r["key"]: r["attempts"] for r in found}


# --- the page by link --------------------------------------------------------------------------


async def test_the_token_of_a_real_checkout_opens_the_view_with_only_customer_keys(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await container.services.get(CancelUnpaidOrder).execute(placed.number, "motivo privado", OWNER)

    found = await view(container, placed.number)

    dumped = found.model_dump(mode="json")
    assert set(dumped) == VIEW_KEYS
    assert set(dumped["lines"][0]) == LINE_KEYS
    assert set(dumped["address"]) == ADDRESS_KEYS
    assert all(set(step) == STEP_KEYS for step in dumped["timeline"])
    assert FORBIDDEN.isdisjoint(dumped)
    text_ = found.model_dump_json()
    stored = await order_row(container, placed.number)
    for secret in (
        "motivo privado",
        OWNER,
        str(placed.reservation_id),
        stored["access_token_digest"],
        stored["cart_token_digest"],
    ):
        assert secret not in text_, secret


async def test_a_wrong_token_is_order_not_found(
    container: Container, catalog_rows: CatalogRows
) -> None:
    await place(container, catalog_rows)

    result = await view_query(container).execute("not-a-token")

    assert isinstance(refused(result), OrderNotFound)  # type: ignore[arg-type]


# --- the lookup --------------------------------------------------------------------------------


async def test_the_lookup_answers_the_token_that_opens_the_same_order(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    email = await customer_email(container, placed.number)

    result = await lookup_command(container).execute(
        f" lfi-{placed.number} ", f" {email.upper()} ", "1.1.1.1"
    )

    assert result == Ok(await token_of(container, placed.number))
    assert isinstance(result, Ok)
    opened = await view_query(container).execute(result.value)
    assert isinstance(opened, Ok)
    assert opened.value.number == f"LFI-{placed.number}"


async def test_a_wrong_email_and_an_unknown_number_answer_equal_errors(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    lookup = lookup_command(container)

    wrong = await lookup.execute(str(placed.number), "otro@example.com", "1.1.1.1")
    unknown = await lookup.execute(
        "9999", await customer_email(container, placed.number), "1.1.1.2"
    )

    assert isinstance(refused(wrong), OrderLookupNotFound)  # type: ignore[arg-type]
    assert wrong == unknown


async def test_the_attempts_are_committed_when_the_sixth_is_refused(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    lookup = lookup_command(container)
    email = await customer_email(container, placed.number)
    answers = [
        await lookup.execute(str(placed.number), "otro@example.com", f"10.0.0.{i}")
        for i in range(LOOKUP_NUMBER_MAX)
    ]
    assert all(isinstance(refused(a), OrderLookupNotFound) for a in answers)  # type: ignore[arg-type]

    sixth = await lookup.execute(str(placed.number), email, "10.0.0.99")

    assert isinstance(refused(sixth), OrderLookupRateLimited)  # type: ignore[arg-type]
    counted = await throttle_rows(container)
    assert counted[f"lookup-number:{placed.number}"] == LOOKUP_NUMBER_MAX + 1
    assert counted["lookup-ip:10.0.0.99"] == 1
    # The refusal kept the count: a seventh is refused too, with the right email.
    seventh = await lookup.execute(str(placed.number), email, "10.0.0.98")
    assert isinstance(refused(seventh), OrderLookupRateLimited)  # type: ignore[arg-type]
    assert (await throttle_rows(container))[
        f"lookup-number:{placed.number}"
    ] == LOOKUP_NUMBER_MAX + 2


async def test_an_unparsable_number_only_counts_the_ip_row(container: Container) -> None:
    await lookup_command(container).execute("abc", EMAIL, "1.1.1.1")

    assert await throttle_rows(container) == {"lookup-ip:1.1.1.1": 1}


# --- the customer's cancel ---------------------------------------------------------------------


async def test_the_customer_cancel_releases_the_real_reservation_and_records_the_actor(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    assert await item_row(container, placed.stock) == (5, 2)

    result = await cancel_command(container).execute(await token_of(container, placed.number))

    assert isinstance(result, Ok), result
    stored = await order_row(container, placed.number)
    assert (stored["status"], stored["cancel_reason"]) == ("cancelled", "Cancelado por el cliente")
    assert (await reservation_row(container, placed.reservation_id))["status"] == "released"  # type: ignore[arg-type]
    assert await item_row(container, placed.stock) == (5, 0)
    [event] = await history(container, placed.number)
    assert (event["from_status"], event["to_status"], event["actor"]) == (
        "pending_payment",
        "cancelled",
        "customer",
    )
    page = await view(container, placed.number)
    assert (page.status, page.cancelled_by, page.can_cancel) == ("cancelled", "customer", False)
    assert "Cancelado por el cliente" not in page.model_dump_json()


async def test_a_paid_order_is_refused_and_its_reservation_stays_committed(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await pay(container, placed)

    result = await cancel_command(container).execute(await token_of(container, placed.number))

    assert isinstance(refused(result), OrderTransitionInvalid)
    assert (await order_row(container, placed.number))["status"] == "paid"
    assert (await reservation_row(container, placed.reservation_id))["status"] == "committed"  # type: ignore[arg-type]
    assert await item_row(container, placed.stock) == (3, 0)
    assert len(await history(container, placed.number)) == 1


async def test_an_unknown_token_cancels_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)

    result = await cancel_command(container).execute("not-a-token")

    assert isinstance(refused(result), OrderNotFound)
    assert (await order_row(container, placed.number))["status"] == "pending_payment"
    assert await item_row(container, placed.stock) == (5, 2)


# --- who cancelled -----------------------------------------------------------------------------


async def test_the_overdue_job_shows_system(
    container: Container, catalog_rows: CatalogRows
) -> None:
    from tests.integration.orders.test_order_lifecycle_flow import backdate_due

    placed = await place(container, catalog_rows)
    await backdate_due(container, placed.number)
    assert await container.services.get(CancelOverdueOrders).run_once() == 1

    page = await view(container, placed.number)

    assert (page.status, page.cancelled_by, page.can_cancel) == ("cancelled", "system", False)


async def test_a_panel_cancellation_shows_shop_without_its_reason(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    ok(
        await container.services.get(CancelUnpaidOrder).execute(
            placed.number, "razón interna", OWNER
        )
    )

    page = await view(container, placed.number)

    assert page.cancelled_by == "shop"
    assert "razón interna" not in page.model_dump_json()


async def test_a_refunded_paid_cancellation_shows_done_and_no_extra_step(
    container: Container, catalog_rows: CatalogRows
) -> None:
    placed = await place(container, catalog_rows)
    await pay(container, placed)
    ok(await container.services.get(CancelPaidOrder).execute(placed.number, "Sin producto", OWNER))
    ok(await container.services.get(MarkOrderRefunded).execute(placed.number, "Hecho", OWNER))
    assert len(await history(container, placed.number)) == 3

    page = await view(container, placed.number)

    assert [s.status for s in page.timeline] == ["pending_payment", "paid", "cancelled"]
    assert (page.refund_status, page.cancelled_by, page.status) == ("done", "shop", "cancelled")


async def test_the_timeline_after_paying_and_shipping_has_times_in_order(
    container: Container, catalog_rows: CatalogRows
) -> None:
    from fragancia_api.modules.orders.application.commands.lifecycle import AdvanceOrder

    placed = await place(container, catalog_rows)
    await pay(container, placed)
    ok(
        await container.services.get(AdvanceOrder).execute(
            placed.number, "shipped", "Estafeta", "123456", OWNER
        )
    )

    page = await view(container, placed.number)

    assert [s.status for s in page.timeline] == ["pending_payment", "paid", "shipped"]
    assert page.timeline[0].at <= page.timeline[1].at <= page.timeline[2].at
    assert (page.shipping_carrier, page.tracking_number, page.can_cancel) == (
        "Estafeta",
        "123456",
        False,
    )
