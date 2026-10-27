"""Payments plan 003 step 3, application: the instructions email (`payment_instructions_email`
and `SendPaymentInstructionsEmail` in order_emails.py) over the fake ports and the recording
email sender. The event is built from the real `OrderPaymentPending` class, so the payload is
what the relay hands over."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
import structlog.testing

from fragancia_api.modules.orders.application.handlers.order_emails import (
    SendPaymentInstructionsEmail,
    money,
    payment_instructions_email,
)
from fragancia_api.modules.orders.domain.order import OrderPaymentPending
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.in_memory import RecordingEmailSender
from tests.unit.orders.test_order_emails import (
    FOOTER,
    LINK,
    LINKS,
    ORDER_ID,
    STOREFRONT,
    FakeQueries,
    Rejecting,
    make_detail,
)

URL = "https://mp.test/ticket/1"
# 2026-10-29 18:00 UTC is 13:00 in Cancún (UTC-5, no daylight saving time).
DUE = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)
TOTAL = money(218_100)


def pending_message(
    *,
    number: int = 1001,
    due_at: datetime = DUE,
    method: str = "efectivo en tienda",
    url: str | None = URL,
) -> EventMessage:
    e = OrderPaymentPending(
        order_id=ORDER_ID, number=number, due_at=due_at, method=method, instructions_url=url
    )
    return EventMessage(id=e.event_id, name=e.name, payload=e.payload(), occurred_at=e.occurred_at)


def handler(queries: FakeQueries, email: RecordingEmailSender) -> SendPaymentInstructionsEmail:
    return SendPaymentInstructionsEmail(
        queries=queries, links=LINKS, email=email, storefront_url=STOREFRONT
    )


# --- the copy ----------------------------------------------------------------------------------


def test_the_subject_names_the_order_number() -> None:
    subject, _ = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", DUE, URL)

    assert subject == "Tu ficha de pago del pedido LFI-1001"


def test_the_body_has_the_amount_the_method_the_cancun_deadline_the_page_and_the_footer() -> None:
    _, body = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", DUE, URL)

    assert body == (
        "Hola Ana López:\n\n"
        f"Para completar tu pedido LFI-1001, paga {TOTAL} con efectivo en tienda "
        "antes del 29/10/2026 a las 13:00.\n"
        f"Consulta la ficha con las instrucciones aquí:\n{URL}\n"
        "\n" + FOOTER
    )


def test_the_amount_is_the_order_total() -> None:
    _, body = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", DUE, None)

    assert "paga $2,181.00 MXN con" in body


def test_without_a_page_the_instructions_line_is_left_out() -> None:
    _, body = payment_instructions_email(make_detail(), LINK, "transferencia SPEI", DUE, None)

    assert "Consulta la ficha" not in body
    assert "con transferencia SPEI antes del 29/10/2026 a las 13:00.\n\n" + FOOTER in body


def test_an_empty_page_counts_as_no_page() -> None:
    _, body = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", DUE, "")

    assert "Consulta la ficha" not in body


def test_the_deadline_is_converted_to_cancun_time_across_midnight() -> None:
    # 03:30 UTC on the 30th is 22:30 on the 29th in Cancún.
    due = datetime(2026, 10, 30, 3, 30, tzinfo=UTC)

    _, body = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", due, None)

    assert "antes del 29/10/2026 a las 22:30." in body


def test_a_deadline_with_another_offset_is_converted_not_printed_as_is() -> None:
    due = datetime(2026, 10, 29, 12, 0, tzinfo=timezone(timedelta(hours=-6)))  # 13:00 in Cancún

    _, body = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", due, None)

    assert "antes del 29/10/2026 a las 13:00." in body


def test_the_email_carries_the_customers_private_link() -> None:
    _, body = payment_instructions_email(make_detail(), LINK, "efectivo en tienda", DUE, None)

    assert body.endswith(FOOTER)
    assert LINK in body


# --- the handler -------------------------------------------------------------------------------


async def test_the_handler_emails_the_customer_with_the_order_read_by_number() -> None:
    email = RecordingEmailSender()
    queries = FakeQueries(make_detail())

    await handler(queries, email)(pending_message())

    [mail] = email.sent
    assert queries.asked == [1001]
    assert mail.to == "cliente@example.com"
    assert mail.subject == "Tu ficha de pago del pedido LFI-1001"
    assert (
        mail.body
        == payment_instructions_email(make_detail(), LINK, "efectivo en tienda", DUE, URL)[1]
    )


async def test_the_handler_uses_the_label_and_the_page_of_the_event() -> None:
    email = RecordingEmailSender()

    await handler(FakeQueries(make_detail()), email)(
        pending_message(method="transferencia SPEI", url=None)
    )

    [mail] = email.sent
    assert "con transferencia SPEI antes del" in mail.body
    assert "Consulta la ficha" not in mail.body


async def test_the_handler_uses_the_deadline_of_the_event_not_the_orders() -> None:
    email = RecordingEmailSender()
    later = DUE + timedelta(hours=5)

    await handler(FakeQueries(make_detail()), email)(pending_message(due_at=later))

    [mail] = email.sent
    assert "antes del 29/10/2026 a las 18:00." in mail.body


async def test_the_handler_builds_the_link_from_the_order_id_of_the_event() -> None:
    email = RecordingEmailSender()
    other = UUID(int=555)
    e = OrderPaymentPending(
        order_id=other, number=1001, due_at=DUE, method="efectivo en tienda", instructions_url=None
    )

    await handler(FakeQueries(make_detail()), email)(
        EventMessage(id=e.event_id, name=e.name, payload=e.payload(), occurred_at=e.occurred_at)
    )

    [mail] = email.sent
    assert f"#token={LINKS.token(other)}" in mail.body
    assert LINKS.token(ORDER_ID) not in mail.body


async def test_the_handler_logs_and_returns_when_the_order_is_missing() -> None:
    email = RecordingEmailSender()

    with structlog.testing.capture_logs() as logs:
        await handler(FakeQueries(), email)(pending_message(number=4040))

    assert email.sent == []
    [log] = [entry for entry in logs if entry["event"] == "orders.email_order_missing"]
    assert log["number"] == 4040


async def test_the_handler_raises_on_a_send_failure_so_the_relay_retries() -> None:
    email = RecordingEmailSender()
    email.fail = True

    with pytest.raises(ConnectionError):
        await handler(FakeQueries(make_detail()), email)(pending_message())


async def test_the_handler_logs_and_skips_a_rejected_address() -> None:
    with structlog.testing.capture_logs() as logs:
        await handler(FakeQueries(make_detail()), Rejecting())(pending_message())

    [log] = [entry for entry in logs if entry["event"] == "orders.email_rejected"]
    assert log["address"] == "cliente@example.com"
