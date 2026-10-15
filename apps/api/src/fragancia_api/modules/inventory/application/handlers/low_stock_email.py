"""Outbox subscriber that emails the active owners when presentations go low on stock.

It runs in the worker, inside the relay's transaction. A send failure raises, so the relay
retries the event; delivery is at-least-once, so a retry can send again to the owners who
already got the email. An owner the mail server permanently rejects (`EmailRejected`) is
logged and skipped, not retried (decision 36); the other owners still get the email.
"""

from collections.abc import Mapping
from typing import cast
from uuid import UUID

import structlog

from fragancia_api.modules.inventory.application.ports import OwnerRecipients, PresentationCatalog
from fragancia_api.shared.application.email import EmailMessage, EmailRejected, EmailSender
from fragancia_api.shared.application.events import EventMessage

_log = structlog.get_logger(__name__)

LOW_STOCK_SUBJECT = "Stock bajo en La Fragancia Ideal"
LOW_STOCK_BODY = (
    "Hola {name}:\n\n"
    "Estas presentaciones llegaron a su nivel de stock bajo:\n\n"
    "{lines}\n\n"
    "Revisa el inventario en el panel para resurtirlas.\n"
)
LOW_STOCK_LINE = "- {label}: {on_hand} en almacén (umbral {threshold})"


class SendLowStockEmail:
    def __init__(
        self, *, catalog: PresentationCatalog, owners: OwnerRecipients, email: EmailSender
    ) -> None:
        self._catalog = catalog
        self._owners = owners
        self._email = email

    async def __call__(self, message: EventMessage) -> None:
        raw_lines = cast("list[Mapping[str, object]]", message.payload["lines"])
        if not raw_lines:
            return
        ids = [UUID(str(line["presentation_id"])) for line in raw_lines]
        described = await self._catalog.describe(ids)
        lines: list[str] = []
        for presentation_id, line in zip(ids, raw_lines, strict=True):
            known = described.get(presentation_id)
            lines.append(
                LOW_STOCK_LINE.format(
                    label=known.label if known is not None else str(presentation_id),
                    on_hand=int(str(line["on_hand"])),
                    threshold=int(str(line["threshold"])),
                )
            )
        owners = await self._owners.active_owners()
        if not owners:
            _log.warning("inventory.low_stock_no_owner", presentations=len(ids))
            return
        for owner in owners:
            try:
                await self._email.send(
                    EmailMessage(
                        to=owner.email,
                        subject=LOW_STOCK_SUBJECT,
                        body=LOW_STOCK_BODY.format(name=owner.name, lines="\n".join(lines)),
                    )
                )
            except EmailRejected:
                _log.warning("inventory.low_stock_email_rejected", address=owner.email)
