"""Plan 006 on PostgreSQL (`fragancia_test`): real checkout and lifecycle commands write the outbox
rows; the real `OutboxRelay` hands both events to the three email handlers built over the real
`SqlOrderQueries`, the real `HmacOrderLinks` and orders' `IdentityOwners` over identity's facade,
and a capturing sender stands in for SMTP. The link token of the email is accepted by the real
`GetCustomerOrder` over `SqlCustomerOrders`."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from fragancia_api.container import Container
from fragancia_api.modules.identity import OwnerDirectory
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.orders.application.commands.lifecycle import (
    AdvanceOrder,
    CancelOverdueOrders,
    CancelPaidOrder,
    CancelUnpaidOrder,
    MarkOrderRefunded,
)
from fragancia_api.modules.orders.application.handlers.order_emails import (
    SendOrderPlacedEmail,
    SendOrderStatusEmail,
    SendPaidOrderToOwners,
)
from fragancia_api.modules.orders.infrastructure.identity_owners import IdentityOwners
from fragancia_api.modules.orders.infrastructure.sql_order_queries import SqlOrderQueries
from fragancia_api.shared.application.email import EmailMessage
from fragancia_api.shared.infrastructure.outbox import EventBus, OutboxRelay, outbox
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.identity.conftest import (
    NOW,
    clean_identity,  # noqa: F401  (autouse fixture: no stray owners get these emails)
    store_user,
)
from tests.integration.orders.test_customer_order_flow import LINKS, cancel_command, view_query
from tests.integration.orders.test_order_lifecycle_flow import (
    OWNER,
    Placed,
    backdate_due,
    ok,
    order_row,
    pay,
    place,
)

pytestmark = pytest.mark.integration

STOREFRONT = "https://tienda.example.test"
ADMIN_WEB = "https://admin.example.test"
RELAY_TIMEOUT = 30
CUSTOMER = "ana@example.com"  # what the checkout helper places orders with
SECRET_REASON = "Sospecha de fraude con la tarjeta"


@dataclass
class Capturing:
    """The `EmailSender` port that keeps what it was asked to send."""

    sent: list[EmailMessage] = field(default_factory=list)

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def to(self, address: str) -> list[EmailMessage]:
        return [m for m in self.sent if m.to == address]


def relay_with(container: Container, sender: Capturing) -> OutboxRelay:
    """The handlers `orders/module.py` registers, with the capturing sender in place of SMTP."""
    queries = SqlOrderQueries(container.database)
    owners = IdentityOwners(container.services.get(OwnerDirectory))
    bus = EventBus()
    bus.subscribe(
        "orders.order.placed",
        SendOrderPlacedEmail(queries=queries, links=LINKS, email=sender, storefront_url=STOREFRONT),
    )
    bus.subscribe(
        "orders.order.status_changed",
        SendOrderStatusEmail(queries=queries, links=LINKS, email=sender, storefront_url=STOREFRONT),
    )
    bus.subscribe(
        "orders.order.status_changed",
        SendPaidOrderToOwners(
            queries=queries, owners=owners, email=sender, admin_web_url=ADMIN_WEB
        ),
    )
    return OutboxRelay(container.database, bus)


@dataclass
class World:
    container: Container
    sender: Capturing
    relay: OutboxRelay

    async def deliver(self) -> int:
        return await asyncio.wait_for(self.relay.relay_batch(), timeout=RELAY_TIMEOUT)

    async def outbox(self) -> list[dict[str, object]]:
        async with self.container.database.reader() as session:
            return [dict(r) for r in (await session.execute(select(outbox))).mappings()]

    async def drain(self) -> list[EmailMessage]:
        """Relay everything pending and return only the emails that relay produced."""
        before = len(self.sender.sent)
        await self.deliver()
        return self.sender.sent[before:]


@pytest.fixture
async def world(container: Container) -> AsyncIterator[World]:
    sender = Capturing()
    yield World(container, sender, relay_with(container, sender))
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE identity.sessions, identity.users CASCADE"))


async def add_user(container: Container, name: str, role: Role, *, active: bool = True) -> str:
    address = f"{name.lower()}@owners.test"
    user = User.create(Email(address), DisplayName(name), role, "h", now=NOW)
    await store_user(container, user)
    if not active:
        user.deactivate()
        async with container.database.engine.begin() as connection:
            found = (
                await connection.execute(
                    text("SELECT count(*) FROM identity.users WHERE id = :id"), {"id": user.id}
                )
            ).scalar_one()
            assert found == 1  # proves the WHERE before the UPDATE
            await connection.execute(
                text("UPDATE identity.users SET is_active = false WHERE id = :id"),
                {"id": user.id},
            )
    return address


def token_in(body: str) -> str:
    [link] = [part for part in body.split() if "#token=" in part]
    assert link.startswith(f"{STOREFRONT}/pedido#token=")
    return link.split("#token=", 1)[1]


async def placed_and_relayed(world: World, catalog_rows: CatalogRows, **kw: object) -> Placed:
    placed = await place(world.container, catalog_rows, **kw)  # type: ignore[arg-type]
    await world.deliver()  # the 'recibido' email (and the stock events, which have no handler)
    return placed


# --- placed ----------------------------------------------------------------------------------


async def test_a_checkout_emails_the_customer_with_the_real_order_and_a_link_the_view_accepts(
    world: World, catalog_rows: CatalogRows
) -> None:
    owner = await add_user(world.container, "Ana", Role.OWNER)
    placed = await place(world.container, catalog_rows)

    await world.deliver()

    [mail] = world.sender.to(CUSTOMER)
    assert mail.subject == "Recibimos tu pedido LFI-1001"
    assert "x2: $5,000.00 MXN" in mail.body
    assert "Subtotal: $6,000.00 MXN" in mail.body
    assert "Entrega: $180.00 MXN" in mail.body
    assert "Total: $6,180.00 MXN" in mail.body
    assert "Envío a domicilio:\n" in mail.body
    stored = await order_row(world.container, placed.number)
    assert str(stored["address_street"]) in mail.body
    assert f"C.P. {stored['address_postal_code']}" in mail.body
    assert "Tiempo de preparación estimado: hasta 10 días." in mail.body
    due = (await order_row(world.container, placed.number))["payment_due_at"]
    shown = due.astimezone(ZoneInfo("America/Mexico_City")).strftime("%d/%m/%Y %H:%M")
    assert f"Tienes hasta {shown} (hora de Ciudad de México) para pagar." in mail.body
    assert world.sender.to(owner) == []  # the owners hear about paid orders only
    opened = await view_query(world.container).execute(token_in(mail.body))
    assert isinstance(opened, Ok)
    assert opened.value.number == "LFI-1001"


# --- paid, and the moves after it ------------------------------------------------------------


async def test_marking_paid_emails_the_customer_and_every_active_owner_once(
    world: World, catalog_rows: CatalogRows
) -> None:
    ana = await add_user(world.container, "Ana", Role.OWNER)
    luis = await add_user(world.container, "Luis", Role.OWNER)
    gone = await add_user(world.container, "Gone", Role.OWNER, active=False)
    staff = await add_user(world.container, "Staff", Role.STAFF)
    placed = await placed_and_relayed(world, catalog_rows)
    before = len(world.sender.sent)

    await pay(world.container, placed)
    await world.deliver()

    new = world.sender.sent[before:]
    [customer_mail] = [m for m in new if m.to == CUSTOMER]
    assert customer_mail.subject == "Confirmamos el pago de tu pedido LFI-1001"
    assert [m.to for m in new if m.to != CUSTOMER] == [ana, luis]
    for address in (ana, luis):
        [mail] = [m for m in new if m.to == address]
        assert mail.subject == "Pedido pagado: LFI-1001 — $6,180.00 MXN"
        assert f"{ADMIN_WEB}/admin/pedidos/LFI-1001" in mail.body
        assert "#token=" not in mail.body
    assert world.sender.to(gone) == [] and world.sender.to(staff) == []


async def test_a_home_order_gets_one_email_per_move_and_shipped_shows_carrier_and_tracking(
    world: World, catalog_rows: CatalogRows
) -> None:
    placed = await placed_and_relayed(world, catalog_rows)
    await pay(world.container, placed)
    await world.deliver()
    advance = world.container.services.get(AdvanceOrder)

    ok(await advance.execute(placed.number, "preparing", None, None, OWNER))
    [preparing] = await world.drain()
    ok(await advance.execute(placed.number, "shipped", "Estafeta", "123456", OWNER))
    [shipped] = await world.drain()
    ok(await advance.execute(placed.number, "delivered", None, None, OWNER))
    [delivered] = await world.drain()

    assert preparing.subject == "Estamos preparando tu pedido LFI-1001"
    assert shipped.subject == "Tu pedido LFI-1001 va en camino"
    assert "Paquetería: Estafeta" in shipped.body
    assert "Número de guía: 123456" in shipped.body
    assert delivered.subject == "Entregamos tu pedido LFI-1001"
    for mail in (preparing, shipped, delivered):
        assert mail.to == CUSTOMER
        token_in(mail.body)


async def test_a_pickup_order_ready_for_pickup_shows_the_pickup_address_and_hours(
    world: World, catalog_rows: CatalogRows
) -> None:
    placed = await placed_and_relayed(world, catalog_rows, method="pickup")
    await pay(world.container, placed)
    await world.deliver()

    ok(
        await world.container.services.get(AdvanceOrder).execute(
            placed.number, "ready_for_pickup", None, None, OWNER
        )
    )
    [mail] = await world.drain()

    assert mail.subject == "Tu pedido LFI-1001 está listo para recoger"
    assert "Calle 1 #2, Cancun\nHorario: Lun-Vie 10-18" in mail.body


# --- cancelled -------------------------------------------------------------------------------


async def test_a_customer_cancel_says_a_tu_solicitud(
    world: World, catalog_rows: CatalogRows
) -> None:
    placed = await placed_and_relayed(world, catalog_rows)
    [recibido] = world.sender.to(CUSTOMER)

    ok(await cancel_command(world.container).execute(token_in(recibido.body)))
    [mail] = await world.drain()

    assert mail.subject == "Tu pedido LFI-1001 fue cancelado"
    assert "fue cancelado a tu solicitud." in mail.body
    assert "reembolsaremos" not in mail.body
    assert placed.number == 1001


async def test_the_overdue_job_cancel_says_no_recibimos_el_pago_a_tiempo(
    world: World, catalog_rows: CatalogRows
) -> None:
    placed = await placed_and_relayed(world, catalog_rows)
    await backdate_due(world.container, placed.number)

    assert await world.container.services.get(CancelOverdueOrders).run_once() == 1
    [mail] = await world.drain()

    assert "fue cancelado porque no recibimos el pago a tiempo." in mail.body


async def test_a_shop_cancel_says_por_la_tienda_and_never_the_typed_reason(
    world: World, catalog_rows: CatalogRows
) -> None:
    placed = await placed_and_relayed(world, catalog_rows)

    ok(
        await world.container.services.get(CancelUnpaidOrder).execute(
            placed.number, SECRET_REASON, OWNER
        )
    )
    [mail] = await world.drain()

    assert "fue cancelado por la tienda." in mail.body
    assert SECRET_REASON not in mail.subject + mail.body
    assert "fraude" not in mail.body.lower()
    assert (await order_row(world.container, placed.number))["cancel_reason"] == SECRET_REASON


async def test_a_paid_cancel_promises_the_refund_and_marking_it_done_sends_no_email(
    world: World, catalog_rows: CatalogRows
) -> None:
    placed = await placed_and_relayed(world, catalog_rows)
    await pay(world.container, placed)
    await world.deliver()

    ok(
        await world.container.services.get(CancelPaidOrder).execute(
            placed.number, SECRET_REASON, OWNER
        )
    )
    [cancelled] = await world.drain()
    ok(await world.container.services.get(MarkOrderRefunded).execute(placed.number, "SPEI", OWNER))
    refund_mail = await world.drain()

    assert "Te reembolsaremos el pago completo." in cancelled.body
    assert SECRET_REASON not in cancelled.subject + cancelled.body
    assert refund_mail == []
    refund_rows = [
        row
        for row in await world.outbox()
        if row["name"] == "orders.order.status_changed"
        and row["payload"]["from_status"] == row["payload"]["to_status"]  # type: ignore[index]
    ]
    assert len(refund_rows) == 1  # the refund event existed and was relayed without an email


# --- outbox ----------------------------------------------------------------------------------


async def test_every_handled_event_is_published_with_no_failure(
    world: World, catalog_rows: CatalogRows
) -> None:
    await add_user(world.container, "Ana", Role.OWNER)
    placed = await placed_and_relayed(world, catalog_rows)
    await pay(world.container, placed)
    await world.deliver()
    ok(
        await world.container.services.get(CancelPaidOrder).execute(
            placed.number, SECRET_REASON, OWNER
        )
    )
    await world.deliver()

    rows = await world.outbox()

    assert {r["name"] for r in rows} >= {"orders.order.placed", "orders.order.status_changed"}
    assert all(r["published_at"] is not None for r in rows)
    assert all(r["failed_at"] is None and r["attempts"] == 0 for r in rows)


async def test_with_no_active_owner_the_paid_event_is_still_published_and_the_customer_emailed(
    world: World, catalog_rows: CatalogRows
) -> None:
    placed = await placed_and_relayed(world, catalog_rows)
    await pay(world.container, placed)

    [customer_mail] = await world.drain()

    assert customer_mail.to == CUSTOMER
    assert all(r["published_at"] is not None for r in await world.outbox())
