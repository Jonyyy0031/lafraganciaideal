"""Outgoing email port. Adapters live in `shared/infrastructure`."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    body: str  # plain text


class EmailRejected(Exception):
    """The server permanently refused this recipient (an SMTP 5xx reply); retrying will not
    help."""


class EmailSender(Protocol):
    """Sends one email. Raises `EmailRejected` when the recipient is permanently refused and
    any other exception on failure; call it only from the worker (an outbox subscriber),
    never inside an HTTP request."""

    async def send(self, message: EmailMessage) -> None: ...
