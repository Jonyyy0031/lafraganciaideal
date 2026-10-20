"""Plan 006, application: the order email templates (every status and delivery method) and the
three outbox subscribers, over fake ports and the recording email sender. The events are built
from the real event classes, so the payload is what the relay hands over. The link token is
checked against the real `GetCustomerOrder` use case and the real `POST /orders/view` route."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
import structlog.testing
from httpx import ASGITransport, AsyncClient

from fragancia_api.main.http import build_app
from fragancia_api.modules.orders.application.handlers.order_emails import (
    CANCEL_REASONS,
    ORDER_PAGE_PATH,
    STATUS_TEMPLATES,
    SendOrderPlacedEmail,
    SendOrderStatusEmail,
    SendPaidOrderToOwners,
    money,
    order_link,
    owners_paid_email,
    placed_email,
)
from fragancia_api.modules.orders.application.ports import Recipient
from fragancia_api.modules.orders.application.queries.customer_order import GetCustomerOrder
from fragancia_api.modules.orders.contracts import (
    AdminOrderAddress,
    AdminOrderDetail,
    AdminOrderEvent,
    AdminOrderLine,
    AdminOrderPage,
)
from fragancia_api.modules.orders.domain.order import OrderPlaced, OrderStatusChanged
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryOwnerRecipients
from fragancia_api.modules.orders.infrastructure.order_links import HmacOrderLinks
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.application.email import EmailMessage, EmailRejected
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import RecordingEmailSender
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.orders.support import NOW, FakeOrderLinks
from tests.unit.orders.test_customer_order import Shop
from tests.unit.orders.test_order_lifecycle_http import OrdersActorResolver

ORDER_ID = UUID(int=901)
STOREFRONT = "https://tienda.example.test"
ADMIN_WEB = "https://admin.example.test"
LINKS = FakeOrderLinks()
LINK = order_link(STOREFRONT, LINKS.token(ORDER_ID))
FOOTER = f"Consulta tu pedido aquí:\n{LINK}\n\nSi tienes dudas, responde a este correo.\n"
INTERNAL_REASON = "Cliente sospechoso, fraude probable"
HOME_ADDRESS = AdminOrderAddress(
    street="Av. Reforma",
    exterior_number="100",
    interior_number="4B",
    neighborhood="Juárez",
    postal_code="77500",
    municipality="Cancún",
    state="CMX",
    references="Portón verde",
)
ADDRESS_TEXT = "Av. Reforma 100 int. 4B, Juárez, C.P. 77500, Cancún, CMX (Portón verde)"
ANA = Recipient(name="Ana", email="ana@owners.test")
LUIS = Recipient(name="Luis", email="luis@owners.test")


def line(label: str = "Eros EDT 100 ml", quantity: int = 2, unit: int = 100_050) -> AdminOrderLine:
    return AdminOrderLine(
        presentation_id=UUID(int=1),
        perfume_id=UUID(int=2),
        label=label,
        ml=100,
        availability="in_stock",
        lead_time_min_days=None,
        lead_time_max_days=None,
        quantity=quantity,
        unit_price_cents=unit,
        regular_price_cents=unit,
        line_total_cents=unit * quantity,
    )


def event(from_: str, to: str, actor: str = "system") -> AdminOrderEvent:
    return AdminOrderEvent(
        at=NOW,
        from_status=from_,
        to_status=to,
        actor=actor,
        note=None,
        carrier=None,
        tracking_number=None,
    )


def make_detail(method: str = "home", **overrides: Any) -> AdminOrderDetail:
    """A two-unit order worth 2001.00 + 180.00 delivery (none for pickup), due at 12:30 UTC."""
    pickup = method == "pickup"
    values: dict[str, object] = {
        "number": "LFI-1001",
        "status": "pending_payment",
        "created_at": NOW,
        "updated_at": NOW,
        "payment_due_at": datetime(2026, 10, 2, 12, 30, tzinfo=UTC),
        "customer_name": "Ana López",
        "customer_email": "cliente@example.com",
        "customer_phone": "9981234567",
        "delivery_method": method,
        "delivery_cents": 0 if pickup else 18_000,
        "address": None if pickup else HOME_ADDRESS,
        "pickup_address": "Calle 1 #2, Cancún" if pickup else None,
        "pickup_hours": "Lun-Vie 10-18" if pickup else None,
        "invoice": None,
        "lines": [line()],
        "subtotal_cents": 200_100,
        "total_cents": 200_100 if pickup else 218_100,
        "lead_time_max_days": None,
        "cancel_reason": None,
        "refund_status": None,
        "shipping_carrier": None,
        "tracking_number": None,
        "reservation_id": None,
        "history": [],
        "notes": [],
    }
    values.update(overrides)
    return AdminOrderDetail(**values)


class FakeQueries:
    """`OrderQueries.detail` over a dict by number; `page` is never used by the emails."""

    def __init__(self, *orders: AdminOrderDetail) -> None:
        self.orders = {int(o.number.removeprefix("LFI-")): o for o in orders}
        self.asked: list[int] = []

    async def detail(self, number: int) -> AdminOrderDetail | None:
        self.asked.append(number)
        return self.orders.get(number)

    async def page(self, status: Any, q: str | None, page: int, size: int) -> AdminOrderPage:
        raise AssertionError("the emails never list orders")


def placed_message(number: int = 1001) -> EventMessage:
    e = OrderPlaced(order_id=ORDER_ID, number=number)
    return EventMessage(id=e.event_id, name=e.name, payload=e.payload(), occurred_at=e.occurred_at)


def status_message(from_: str, to: str, number: int = 1001) -> EventMessage:
    e = OrderStatusChanged(order_id=ORDER_ID, number=number, from_status=from_, to_status=to)
    return EventMessage(id=e.event_id, name=e.name, payload=e.payload(), occurred_at=e.occurred_at)


def status_handler(queries: FakeQueries, email: RecordingEmailSender) -> SendOrderStatusEmail:
    return SendOrderStatusEmail(
        queries=queries, links=LINKS, email=email, storefront_url=STOREFRONT
    )


def placed_handler(queries: FakeQueries, email: RecordingEmailSender) -> SendOrderPlacedEmail:
    return SendOrderPlacedEmail(
        queries=queries, links=LINKS, email=email, storefront_url=STOREFRONT
    )


def owners_handler(
    queries: FakeQueries, email: RecordingEmailSender, *owners: Recipient
) -> SendPaidOrderToOwners:
    return SendPaidOrderToOwners(
        queries=queries,
        owners=InMemoryOwnerRecipients(owners),
        email=email,
        admin_web_url=ADMIN_WEB,
    )


# --- helpers ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (0, "$0.00 MXN"),
        (5, "$0.05 MXN"),
        (189_950, "$1,899.50 MXN"),
        (123_456_789, "$1,234,567.89 MXN"),
    ],
)
def test_money_is_formatted_with_thousands_cents_and_the_currency(cents: int, text: str) -> None:
    assert money(cents) == text


def test_the_link_puts_the_token_in_the_fragment_of_the_order_page() -> None:
    assert ORDER_PAGE_PATH == "/pedido"
    assert order_link("https://x.test", "abc") == "https://x.test/pedido#token=abc"


# --- placed ----------------------------------------------------------------------------------


def test_placed_lists_lines_totals_address_deadline_and_the_link() -> None:
    subject, body = placed_email(make_detail("home"), LINK)

    assert subject == "Recibimos tu pedido LFI-1001"
    assert body.startswith("Hola Ana López:\n\n")
    assert "- Eros EDT 100 ml x2: $2,001.00 MXN" in body
    assert "Subtotal: $2,001.00 MXN" in body
    assert "Entrega: $180.00 MXN" in body
    assert "Total: $2,181.00 MXN" in body
    assert f"Envío a domicilio:\n{ADDRESS_TEXT}" in body
    assert "Tienes hasta 02/10/2026 06:30 (hora de Ciudad de México) para pagar." in body
    assert body.endswith(FOOTER)


def test_placed_for_a_local_order_names_the_method_and_shows_the_address() -> None:
    _, body = placed_email(make_detail("local"), LINK)

    assert f"Entrega local:\n{ADDRESS_TEXT}" in body


def test_placed_for_pickup_shows_the_pickup_point_and_hours_and_no_address() -> None:
    _, body = placed_email(make_detail("pickup"), LINK)

    assert "Recoger en tienda:\nCalle 1 #2, Cancún\nHorario: Lun-Vie 10-18" in body
    assert "Av. Reforma" not in body
    assert "Entrega: $0.00 MXN" in body


def test_placed_shows_the_longest_lead_time_only_when_there_is_one() -> None:
    _, with_lead = placed_email(make_detail(lead_time_max_days=10), LINK)
    _, without = placed_email(make_detail(), LINK)

    assert "Tiempo de preparación estimado: hasta 10 días." in with_lead
    assert "Tiempo de preparación" not in without


@pytest.mark.parametrize(
    ("due", "shown"),
    [
        (datetime(2026, 10, 3, 0, 15, tzinfo=UTC), "02/10/2026 18:15"),  # UTC is already tomorrow
        (datetime(2026, 10, 3, 5, 59, tzinfo=UTC), "02/10/2026 23:59"),
        (datetime(2026, 10, 3, 6, 0, tzinfo=UTC), "03/10/2026 00:00"),
        (datetime(2026, 1, 1, 5, 30, tzinfo=UTC), "31/12/2025 23:30"),  # crosses the year too
    ],
)
def test_the_deadline_is_shown_in_mexico_city_time_even_when_utc_crossed_midnight(
    due: datetime, shown: str
) -> None:
    _, body = placed_email(make_detail(payment_due_at=due), LINK)

    assert f"Tienes hasta {shown} (hora de Ciudad de México) para pagar." in body


def test_a_deadline_with_another_offset_is_converted_not_printed_as_is() -> None:
    due = datetime(2026, 10, 2, 20, 0, tzinfo=timezone(timedelta(hours=2)))  # 12:00 in Mexico City

    _, body = placed_email(make_detail(payment_due_at=due), LINK)

    assert "Tienes hasta 02/10/2026 12:00" in body


# --- paid / preparing / shipped / pickup / delivered -----------------------------------------


@pytest.mark.parametrize(
    ("method", "next_step"),
    [
        ("home", "Lo prepararemos y te avisaremos cuando salga a su destino."),
        ("local", "Lo prepararemos y te avisaremos cuando salga a su destino."),
        ("pickup", "Te avisaremos en cuanto esté listo para recoger."),
    ],
)
def test_paid_confirms_the_payment_with_the_next_step_of_the_method(
    method: str, next_step: str
) -> None:
    subject, body = STATUS_TEMPLATES["paid"](make_detail(method), LINK)

    assert subject == "Confirmamos el pago de tu pedido LFI-1001"
    assert "Confirmamos el pago de tu pedido LFI-1001." in body
    assert next_step in body
    assert LINK in body


def test_preparing_shows_the_lead_time_only_when_there_is_one() -> None:
    subject, with_lead = STATUS_TEMPLATES["preparing"](make_detail(lead_time_max_days=7), LINK)
    _, without = STATUS_TEMPLATES["preparing"](make_detail(), LINK)

    assert subject == "Estamos preparando tu pedido LFI-1001"
    assert "Tiempo de preparación estimado: hasta 7 días." in with_lead
    assert "Tiempo de preparación" not in without
    assert LINK in with_lead


def test_shipped_home_shows_the_carrier_and_the_tracking_number() -> None:
    order = make_detail("home", shipping_carrier="Estafeta", tracking_number="123456")

    subject, body = STATUS_TEMPLATES["shipped"](order, LINK)

    assert subject == "Tu pedido LFI-1001 va en camino"
    assert "Paquetería: Estafeta\n" in body
    assert "Número de guía: 123456\n" in body
    assert LINK in body


def test_shipped_local_has_no_tracking_number() -> None:
    subject, body = STATUS_TEMPLATES["shipped"](make_detail("local"), LINK)

    assert subject == "Tu pedido LFI-1001 va en camino"
    assert "Número de guía" not in body
    assert "Paquetería" not in body
    assert LINK in body


def test_ready_for_pickup_shows_the_pickup_address_and_hours() -> None:
    subject, body = STATUS_TEMPLATES["ready_for_pickup"](make_detail("pickup"), LINK)

    assert subject == "Tu pedido LFI-1001 está listo para recoger"
    assert "Calle 1 #2, Cancún\nHorario: Lun-Vie 10-18" in body
    assert LINK in body


def test_delivered_thanks_the_customer_and_carries_the_link() -> None:
    subject, body = STATUS_TEMPLATES["delivered"](make_detail(), LINK)

    assert subject == "Entregamos tu pedido LFI-1001"
    assert "Entregamos tu pedido LFI-1001." in body
    assert LINK in body


@pytest.mark.parametrize("status", sorted(STATUS_TEMPLATES))
@pytest.mark.parametrize("method", ["home", "local", "pickup"])
def test_every_customer_template_greets_by_name_and_ends_with_the_link_footer(
    status: str, method: str
) -> None:
    _, body = STATUS_TEMPLATES[status](make_detail(method), LINK)

    assert body.startswith("Hola Ana López:\n\n")
    assert body.endswith(FOOTER)


def test_the_customer_templates_cover_exactly_the_six_statuses_after_pending() -> None:
    assert set(STATUS_TEMPLATES) == {
        "paid",
        "preparing",
        "shipped",
        "ready_for_pickup",
        "delivered",
        "cancelled",
    }


# --- cancelled -------------------------------------------------------------------------------


def cancelled_order(actor: str, **overrides: Any) -> AdminOrderDetail:
    return make_detail(
        status="cancelled",
        cancel_reason=INTERNAL_REASON,
        history=[event("pending_payment", "cancelled", actor)],
        **overrides,
    )


@pytest.mark.parametrize(
    ("actor", "reason"),
    [
        ("customer", "a tu solicitud"),
        ("system", "porque no recibimos el pago a tiempo"),
        ("admin-user-7", "por la tienda"),
    ],
)
def test_cancelled_wording_comes_from_who_cancelled(actor: str, reason: str) -> None:
    subject, body = STATUS_TEMPLATES["cancelled"](cancelled_order(actor), LINK)

    assert subject == "Tu pedido LFI-1001 fue cancelado"
    assert f"Tu pedido LFI-1001 fue cancelado {reason}.\n" in body
    assert LINK in body


@pytest.mark.parametrize("actor", ["customer", "system", "admin-user-7"])
def test_the_typed_cancel_reason_never_appears_in_a_cancelled_email(actor: str) -> None:
    order = cancelled_order(actor, refund_status="pending")

    subject, body = STATUS_TEMPLATES["cancelled"](order, LINK)

    assert INTERNAL_REASON not in subject + body
    assert "fraude" not in (subject + body).lower()


def test_the_cancel_reason_is_absent_from_every_customer_template() -> None:
    for status, template in STATUS_TEMPLATES.items():
        order = (
            cancelled_order("admin-user-7")
            if status == "cancelled"
            else make_detail(cancel_reason=INTERNAL_REASON)
        )
        subject, body = template(order, LINK)
        assert INTERNAL_REASON not in subject + body, status
    subject, body = placed_email(make_detail(cancel_reason=INTERNAL_REASON), LINK)
    assert INTERNAL_REASON not in subject + body


def test_the_cancel_wording_table_has_the_three_actors() -> None:
    assert CANCEL_REASONS == {
        "customer": "a tu solicitud",
        "system": "porque no recibimos el pago a tiempo",
        "shop": "por la tienda",
    }


def test_a_pending_refund_adds_the_refund_sentence() -> None:
    order = cancelled_order("admin-1", refund_status="pending")

    _, body = STATUS_TEMPLATES["cancelled"](order, LINK)

    assert "Te reembolsaremos el pago completo.\n" in body


@pytest.mark.parametrize("refund", [None, "done"])
def test_without_a_pending_refund_there_is_no_refund_sentence(refund: str | None) -> None:
    _, body = STATUS_TEMPLATES["cancelled"](cancelled_order("admin-1", refund_status=refund), LINK)

    assert "reembolsaremos" not in body


def test_after_the_refund_mark_the_wording_still_follows_the_cancelling_move() -> None:
    """The history ends with the refund row (cancelled -> cancelled, by an admin); the reason is
    still the one of the move that cancelled the order."""
    order = make_detail(
        status="cancelled",
        history=[
            event("pending_payment", "paid", "admin-1"),
            event("paid", "cancelled", "customer"),
            event("cancelled", "cancelled", "admin-2"),
        ],
    )

    _, body = STATUS_TEMPLATES["cancelled"](order, LINK)

    assert "fue cancelado a tu solicitud." in body


# --- owners ----------------------------------------------------------------------------------


def test_the_owner_email_has_the_total_customer_lines_method_and_admin_link() -> None:
    subject, body = owners_paid_email(make_detail("home"), ADMIN_WEB, "Ana")

    assert subject == "Pedido pagado: LFI-1001 — $2,181.00 MXN"
    assert body.startswith("Hola Ana:\n\n")
    assert "Cliente: Ana López\nCorreo: cliente@example.com\nTeléfono: 9981234567\n" in body
    assert "- Eros EDT 100 ml x2: $2,001.00 MXN" in body
    assert f"Envío a domicilio:\n{ADDRESS_TEXT}" in body
    assert body.endswith(f"Ábrelo en el panel:\n{ADMIN_WEB}/admin/pedidos/LFI-1001\n")


def test_the_owner_email_for_pickup_shows_the_pickup_point() -> None:
    _, body = owners_paid_email(make_detail("pickup"), ADMIN_WEB, "Ana")

    assert "Recoger en tienda:\nCalle 1 #2, Cancún\nHorario: Lun-Vie 10-18" in body


def test_the_owner_email_has_no_customer_private_link() -> None:
    _, body = owners_paid_email(make_detail(), ADMIN_WEB, "Ana")

    assert "#token=" not in body
    assert f"{ORDER_PAGE_PATH}#" not in body


# --- SendOrderPlacedEmail --------------------------------------------------------------------


async def test_the_placed_handler_emails_the_customer_with_the_order_read_by_number() -> None:
    email = RecordingEmailSender()
    queries = FakeQueries(make_detail())

    await placed_handler(queries, email)(placed_message())

    [mail] = email.sent
    assert queries.asked == [1001]
    assert mail.to == "cliente@example.com"
    assert mail.subject == "Recibimos tu pedido LFI-1001"
    assert mail.body == placed_email(make_detail(), LINK)[1]


async def test_the_placed_handler_logs_and_returns_when_the_order_is_missing() -> None:
    email = RecordingEmailSender()

    with structlog.testing.capture_logs() as logs:
        await placed_handler(FakeQueries(), email)(placed_message(4040))

    assert email.sent == []
    [log] = [entry for entry in logs if entry["event"] == "orders.email_order_missing"]
    assert log["number"] == 4040


async def test_the_placed_handler_raises_on_a_send_failure_so_the_relay_retries() -> None:
    email = RecordingEmailSender()
    email.fail = True

    with pytest.raises(ConnectionError):
        await placed_handler(FakeQueries(make_detail()), email)(placed_message())


class Rejecting(RecordingEmailSender):
    async def send(self, message: EmailMessage) -> None:
        raise EmailRejected("550 mailbox unavailable")


async def test_the_placed_handler_logs_and_skips_a_rejected_address() -> None:
    with structlog.testing.capture_logs() as logs:
        await placed_handler(FakeQueries(make_detail()), Rejecting())(placed_message())

    [log] = [entry for entry in logs if entry["event"] == "orders.email_rejected"]
    assert log["address"] == "cliente@example.com"


# --- SendOrderStatusEmail --------------------------------------------------------------------

MOVES = [
    ("pending_payment", "paid"),
    ("paid", "preparing"),
    ("paid", "shipped"),
    ("preparing", "shipped"),
    ("paid", "ready_for_pickup"),
    ("shipped", "delivered"),
    ("pending_payment", "cancelled"),
]


@pytest.mark.parametrize(("from_", "to"), MOVES)
async def test_a_move_sends_the_template_of_its_to_status_to_the_customer(
    from_: str, to: str
) -> None:
    email = RecordingEmailSender()
    order = cancelled_order("admin-1") if to == "cancelled" else make_detail()

    await status_handler(FakeQueries(order), email)(status_message(from_, to))

    [mail] = email.sent
    assert mail.to == "cliente@example.com"
    assert (mail.subject, mail.body) == STATUS_TEMPLATES[to](order, LINK)


async def test_the_refund_row_cancelled_to_cancelled_sends_nothing_and_reads_nothing() -> None:
    email = RecordingEmailSender()
    queries = FakeQueries(cancelled_order("admin-1", refund_status="done"))

    await status_handler(queries, email)(status_message("cancelled", "cancelled"))

    assert email.sent == []
    assert queries.asked == []


async def test_a_status_without_a_template_sends_nothing() -> None:
    email = RecordingEmailSender()

    await status_handler(FakeQueries(make_detail()), email)(
        status_message("paid", "pending_payment")
    )

    assert email.sent == []


async def test_the_status_handler_logs_and_returns_when_the_order_is_missing() -> None:
    email = RecordingEmailSender()

    with structlog.testing.capture_logs() as logs:
        await status_handler(FakeQueries(), email)(status_message("pending_payment", "paid"))

    assert email.sent == []
    assert [e for e in logs if e["event"] == "orders.email_order_missing"] != []


async def test_the_status_handler_raises_on_a_send_failure() -> None:
    email = RecordingEmailSender()
    email.fail = True

    with pytest.raises(ConnectionError):
        await status_handler(FakeQueries(make_detail()), email)(
            status_message("pending_payment", "paid")
        )


async def test_the_status_handler_logs_and_skips_a_rejected_address() -> None:
    with structlog.testing.capture_logs() as logs:
        await status_handler(FakeQueries(make_detail()), Rejecting())(
            status_message("pending_payment", "paid")
        )

    assert [e for e in logs if e["event"] == "orders.email_rejected"] != []


async def test_a_shop_cancel_email_sent_by_the_handler_never_carries_the_typed_reason() -> None:
    email = RecordingEmailSender()
    order = cancelled_order("admin-1", refund_status="pending")

    await status_handler(FakeQueries(order), email)(status_message("paid", "cancelled"))

    [mail] = email.sent
    assert INTERNAL_REASON not in mail.subject + mail.body
    assert "por la tienda" in mail.body


# --- SendPaidOrderToOwners -------------------------------------------------------------------


async def test_every_active_owner_gets_one_email_when_the_order_is_paid() -> None:
    email = RecordingEmailSender()

    await owners_handler(FakeQueries(make_detail()), email, ANA, LUIS)(
        status_message("pending_payment", "paid")
    )

    assert [m.to for m in email.sent] == ["ana@owners.test", "luis@owners.test"]
    assert email.sent[0].body.startswith("Hola Ana:\n")
    assert email.sent[1].body.startswith("Hola Luis:\n")
    assert {m.subject for m in email.sent} == {"Pedido pagado: LFI-1001 — $2,181.00 MXN"}


@pytest.mark.parametrize(
    ("from_", "to"), [m for m in MOVES if m[1] != "paid"] + [("cancelled", "cancelled")]
)
async def test_the_owners_hear_only_about_paid_never_about_other_moves(from_: str, to: str) -> None:
    email = RecordingEmailSender()
    queries = FakeQueries(make_detail())

    await owners_handler(queries, email, ANA)(status_message(from_, to))

    assert email.sent == []
    assert queries.asked == []


async def test_with_no_active_owner_nothing_is_sent_and_it_is_logged() -> None:
    email = RecordingEmailSender()

    with structlog.testing.capture_logs() as logs:
        await owners_handler(FakeQueries(make_detail()), email)(
            status_message("pending_payment", "paid")
        )

    assert email.sent == []
    [log] = [e for e in logs if e["event"] == "orders.paid_email_no_owner"]
    assert log["number"] == 1001


async def test_the_owner_handler_logs_and_returns_when_the_order_is_missing() -> None:
    email = RecordingEmailSender()

    with structlog.testing.capture_logs() as logs:
        await owners_handler(FakeQueries(), email, ANA)(status_message("pending_payment", "paid"))

    assert email.sent == []
    assert [e for e in logs if e["event"] == "orders.email_order_missing"] != []


async def test_the_owner_handler_raises_on_a_send_failure() -> None:
    email = RecordingEmailSender()
    email.fail = True

    with pytest.raises(ConnectionError):
        await owners_handler(FakeQueries(make_detail()), email, ANA)(
            status_message("pending_payment", "paid")
        )


async def test_a_rejected_owner_is_logged_and_skipped_and_the_next_still_gets_it() -> None:
    class RejectsAna(RecordingEmailSender):
        async def send(self, message: EmailMessage) -> None:
            if message.to == ANA.email:
                raise EmailRejected("550")
            await super().send(message)

    email = RejectsAna()

    with structlog.testing.capture_logs() as logs:
        await owners_handler(FakeQueries(make_detail()), email, ANA, LUIS)(
            status_message("pending_payment", "paid")
        )

    assert [m.to for m in email.sent] == ["luis@owners.test"]
    [log] = [e for e in logs if e["event"] == "orders.paid_email_rejected"]
    assert log["address"] == "ana@owners.test"


# --- the link token opens the order through the real view ------------------------------------

SECRET = "s" * 32


def token_in(body: str) -> str:
    [link] = [part for part in body.split() if "#token=" in part]
    return link.split("#token=", 1)[1]


async def stored_order_and_its_placed_email(secret: str = SECRET) -> tuple[Shop, str]:
    """An order stored by the lifecycle world with the access digest `PlaceOrder` would have
    stored for its id, and the body of the 'recibido' email the handler builds for it."""
    links = HmacOrderLinks(secret)
    shop = Shop()
    order = await shop.store()
    shop.world.current(1).access_token_digest = links.digest(links.token(order.id))
    email = RecordingEmailSender()
    handler = SendOrderPlacedEmail(
        queries=FakeQueries(make_detail()),
        links=links,
        email=email,
        storefront_url=STOREFRONT,
    )
    event_ = OrderPlaced(order_id=order.id, number=order.number)
    await handler(
        EventMessage(
            id=event_.event_id, name=event_.name, payload=event_.payload(), occurred_at=NOW
        )
    )
    [mail] = email.sent
    return shop, mail.body


async def test_the_token_in_the_email_link_is_accepted_by_the_get_customer_order_use_case() -> None:
    shop, body = await stored_order_and_its_placed_email()

    result = await GetCustomerOrder(queries=shop.customer, links=HmacOrderLinks(SECRET)).execute(
        token_in(body)
    )

    assert isinstance(result, Ok)
    assert result.value.number == "LFI-1001"


async def test_a_tampered_token_from_the_email_link_is_not_accepted() -> None:
    shop, body = await stored_order_and_its_placed_email()

    result = await GetCustomerOrder(queries=shop.customer, links=HmacOrderLinks(SECRET)).execute(
        token_in(body) + "x"
    )

    assert isinstance(result, Err)


async def test_the_token_in_the_email_link_is_accepted_by_the_post_orders_view_route() -> None:
    shop, body = await stored_order_and_its_placed_email()
    services = ServiceRegistry()
    services.add(ActorResolver, OrdersActorResolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        GetCustomerOrder, GetCustomerOrder(queries=shop.customer, links=HmacOrderLinks(SECRET))
    )
    app = build_app(services, routers)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/api/v1/orders/view", json={"access_token": token_in(body)})

    assert response.status_code == 200
    assert response.json()["number"] == "LFI-1001"
