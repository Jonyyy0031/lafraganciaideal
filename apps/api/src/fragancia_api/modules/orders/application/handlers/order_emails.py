"""Outbox subscribers that email the customer and the owners about an order (decisions 12, 44,
45 and 46).

They run in the worker, inside the relay's transaction, and read the order's current data
through `OrderQueries.detail`. Plain text, in Spanish. A send failure raises, so the relay
retries the event; delivery is at-least-once, so a retry can send the same email twice. An
address the mail server permanently rejects (`EmailRejected`) is logged and skipped.
Customer emails never carry the shop's internal cancel reason (decision 40).
"""

from collections.abc import Callable
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog

from fragancia_api.modules.orders.application.ports import (
    OrderLinks,
    OrderQueries,
    OwnerRecipients,
)
from fragancia_api.modules.orders.contracts import AdminOrderAddress, AdminOrderDetail
from fragancia_api.modules.orders.domain.order import PAYMENTS_ACTOR, cancelled_by
from fragancia_api.shared.application.email import EmailMessage, EmailRejected, EmailSender
from fragancia_api.shared.application.events import EventMessage

_log = structlog.get_logger(__name__)

ORDER_PAGE_PATH = "/pedido"
# The shop's zone, as catalog decision 66 fixed it (UTC-5 all year); decision 48.
SHOP_TIMEZONE = ZoneInfo("America/Cancun")

FOOTER = "Consulta tu pedido aquí:\n{link}\n\nSi tienes dudas, responde a este correo.\n"
GREETING = "Hola {name}:\n\n"
CANCEL_REASONS = {
    "customer": "a tu solicitud",
    "system": "porque no recibimos el pago a tiempo",
    "shop": "por la tienda",
}
STOCK_GONE_TEXT = (
    "Tu pedido {number} fue cancelado porque tu pago llegó cuando ya no había existencias.\n"
    "Te reembolsaremos el pago completo.\n"
)
DELIVERY_NAMES = {
    "home": "Envío a domicilio",
    "local": "Entrega local",
    "pickup": "Recoger en tienda",
}

Template = tuple[str, str]


def order_link(storefront_url: str, token: str) -> str:
    return f"{storefront_url}{ORDER_PAGE_PATH}#token={token}"


def money(cents: int) -> str:
    return f"${cents / 100:,.2f} MXN"


def _address(address: AdminOrderAddress) -> str:
    street = f"{address.street} {address.exterior_number}"
    if address.interior_number:
        street += f" int. {address.interior_number}"
    text = (
        f"{street}, {address.neighborhood}, C.P. {address.postal_code}, "
        f"{address.municipality}, {address.state}"
    )
    if address.references:
        text += f" ({address.references})"
    return text


def _pickup(order: AdminOrderDetail) -> str:
    return f"{order.pickup_address or ''}\nHorario: {order.pickup_hours or ''}"


def _delivery(order: AdminOrderDetail) -> str:
    name = DELIVERY_NAMES[order.delivery_method]
    if order.delivery_method == "pickup":
        return f"{name}:\n{_pickup(order)}"
    if order.address is not None:
        return f"{name}:\n{_address(order.address)}"
    return name


def _lines(order: AdminOrderDetail) -> str:
    return "\n".join(
        f"- {line.label} x{line.quantity}: {money(line.line_total_cents)}" for line in order.lines
    )


def _lead_time(order: AdminOrderDetail) -> str:
    if order.lead_time_max_days is None:
        return ""
    days = order.lead_time_max_days
    unit = "día" if days == 1 else "días"
    return f"Tiempo de preparación estimado: hasta {days} {unit}.\n"


def _wrap(order: AdminOrderDetail, link: str, text: str) -> str:
    return GREETING.format(name=order.customer_name) + text + "\n" + FOOTER.format(link=link)


def placed_email(order: AdminOrderDetail, link: str) -> Template:
    due = order.payment_due_at.astimezone(SHOP_TIMEZONE).strftime("%d/%m/%Y %H:%M")
    text = (
        "Recibimos tu pedido. Esto es lo que pediste:\n\n"
        f"{_lines(order)}\n\n"
        f"Subtotal: {money(order.subtotal_cents)}\n"
        f"Entrega: {money(order.delivery_cents)}\n"
        f"Total: {money(order.total_cents)}\n\n"
        f"{_delivery(order)}\n\n"
        f"Tienes hasta {due} (hora de Cancún) para pagar.\n"
        f"{_lead_time(order)}"
    )
    return f"Recibimos tu pedido {order.number}", _wrap(order, link, text)


def paid_email(order: AdminOrderDetail, link: str) -> Template:
    if order.delivery_method == "pickup":
        step = "Te avisaremos en cuanto esté listo para recoger."
    else:
        step = "Lo prepararemos y te avisaremos cuando salga a su destino."
    text = f"Confirmamos el pago de tu pedido {order.number}.\n{step}\n"
    return f"Confirmamos el pago de tu pedido {order.number}", _wrap(order, link, text)


def preparing_email(order: AdminOrderDetail, link: str) -> Template:
    text = f"Estamos preparando tu pedido {order.number}.\n{_lead_time(order)}"
    return f"Estamos preparando tu pedido {order.number}", _wrap(order, link, text)


def shipped_email(order: AdminOrderDetail, link: str) -> Template:
    text = f"Tu pedido {order.number} va en camino.\n"
    if order.shipping_carrier:
        text += f"Paquetería: {order.shipping_carrier}\n"
    if order.tracking_number:
        text += f"Número de guía: {order.tracking_number}\n"
    return f"Tu pedido {order.number} va en camino", _wrap(order, link, text)


def ready_for_pickup_email(order: AdminOrderDetail, link: str) -> Template:
    text = f"Tu pedido {order.number} está listo para recoger.\n\n{_pickup(order)}\n"
    return f"Tu pedido {order.number} está listo para recoger", _wrap(order, link, text)


def delivered_email(order: AdminOrderDetail, link: str) -> Template:
    text = f"Entregamos tu pedido {order.number}. Gracias por comprar con nosotros.\n"
    return f"Entregamos tu pedido {order.number}", _wrap(order, link, text)


def cancelled_actor(order: AdminOrderDetail) -> str:
    """The actor of the move that cancelled the order (the refund mark is not that move)."""
    for event in reversed(order.history):
        if event.to_status == "cancelled" and event.from_status != "cancelled":
            return event.actor
    return "system"


def cancelled_email(order: AdminOrderDetail, link: str) -> Template:
    actor = cancelled_actor(order)
    if actor == PAYMENTS_ACTOR:
        # A late payment that found no stock (payments decision 13): the customer paid.
        text = STOCK_GONE_TEXT.format(number=order.number)
        return f"Tu pedido {order.number} fue cancelado", _wrap(order, link, text)
    reason = CANCEL_REASONS[cancelled_by(actor)]
    text = f"Tu pedido {order.number} fue cancelado {reason}.\n"
    if order.refund_status == "pending":
        text += "Te reembolsaremos el pago completo.\n"
    return f"Tu pedido {order.number} fue cancelado", _wrap(order, link, text)


def owners_paid_email(order: AdminOrderDetail, admin_web_url: str, owner_name: str) -> Template:
    text = (
        f"Hola {owner_name}:\n\n"
        f"Se pagó el pedido {order.number} por {money(order.total_cents)}.\n\n"
        f"Cliente: {order.customer_name}\n"
        f"Correo: {order.customer_email}\n"
        f"Teléfono: {order.customer_phone}\n\n"
        f"{_lines(order)}\n\n"
        f"{_delivery(order)}\n\n"
        f"Ábrelo en el panel:\n{admin_web_url}/admin/pedidos/{order.number}\n"
    )
    return f"Pedido pagado: {order.number} — {money(order.total_cents)}", text


STATUS_TEMPLATES: dict[str, Callable[[AdminOrderDetail, str], Template]] = {
    "paid": paid_email,
    "preparing": preparing_email,
    "shipped": shipped_email,
    "ready_for_pickup": ready_for_pickup_email,
    "delivered": delivered_email,
    "cancelled": cancelled_email,
}


async def _send(email: EmailSender, to: str, template: Template, event: str) -> None:
    subject, body = template
    try:
        await email.send(EmailMessage(to=to, subject=subject, body=body))
    except EmailRejected:
        _log.warning(event, address=to)


class SendOrderPlacedEmail:
    """`orders.order.placed` to the customer's "pedido recibido" email."""

    def __init__(
        self, *, queries: OrderQueries, links: OrderLinks, email: EmailSender, storefront_url: str
    ) -> None:
        self._queries = queries
        self._links = links
        self._email = email
        self._storefront_url = storefront_url

    async def __call__(self, message: EventMessage) -> None:
        number = int(str(message.payload["number"]))
        order = await self._queries.detail(number)
        if order is None:
            _log.warning("orders.email_order_missing", number=number)
            return
        link = order_link(
            self._storefront_url, self._links.token(UUID(str(message.payload["order_id"])))
        )
        await _send(
            self._email,
            order.customer_email,
            placed_email(order, link),
            "orders.email_rejected",
        )


class SendOrderStatusEmail:
    """`orders.order.status_changed` to one customer email per status."""

    def __init__(
        self, *, queries: OrderQueries, links: OrderLinks, email: EmailSender, storefront_url: str
    ) -> None:
        self._queries = queries
        self._links = links
        self._email = email
        self._storefront_url = storefront_url

    async def __call__(self, message: EventMessage) -> None:
        to_status = str(message.payload["to_status"])
        if str(message.payload["from_status"]) == to_status:
            return  # the refund mark: no email (decision 46)
        template = STATUS_TEMPLATES.get(to_status)
        if template is None:
            return
        number = int(str(message.payload["number"]))
        order = await self._queries.detail(number)
        if order is None:
            _log.warning("orders.email_order_missing", number=number)
            return
        link = order_link(
            self._storefront_url, self._links.token(UUID(str(message.payload["order_id"])))
        )
        await _send(
            self._email, order.customer_email, template(order, link), "orders.email_rejected"
        )


class SendPaidOrderToOwners:
    """`orders.order.paid`: one email per active owner (its own outbox row, decision 47)."""

    def __init__(
        self,
        *,
        queries: OrderQueries,
        owners: OwnerRecipients,
        email: EmailSender,
        admin_web_url: str,
    ) -> None:
        self._queries = queries
        self._owners = owners
        self._email = email
        self._admin_web_url = admin_web_url

    async def __call__(self, message: EventMessage) -> None:
        number = int(str(message.payload["number"]))
        order = await self._queries.detail(number)
        if order is None:
            _log.warning("orders.email_order_missing", number=number)
            return
        owners = await self._owners.active_owners()
        if not owners:
            _log.warning("orders.paid_email_no_owner", number=number)
            return
        for owner in owners:
            await _send(
                self._email,
                owner.email,
                owners_paid_email(order, self._admin_web_url, owner.name),
                "orders.paid_email_rejected",
            )
