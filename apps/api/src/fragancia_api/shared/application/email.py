"""Outgoing email port. Adapters live in `shared/infrastructure`."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    body: str  # plain text


class EmailSender(Protocol):
    """Sends one email. Raises on failure; call it only from the worker (an outbox subscriber),
    never inside an HTTP request."""

    async def send(self, message: EmailMessage) -> None: ...
