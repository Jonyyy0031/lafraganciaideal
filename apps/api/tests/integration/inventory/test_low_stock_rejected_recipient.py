"""Plan 004, repair round 1 (decision 36): a recipient the mail server refuses is skipped, not
retried, and the other owners still get the email.

Mailpit accepts every recipient and cannot be told to refuse one, so the refusal comes from a tiny
SMTP server on a loopback port (`StubSmtp`) that answers `RCPT TO` with a chosen reply. The real
`SmtpEmailSender` and the real `smtplib` talk to it, so the exceptions are the ones `smtplib`
raises from real replies, not hand-built ones. Every call is bounded by `TIMEOUT`."""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import structlog.testing

from fragancia_api.config import Settings
from fragancia_api.container import Container, build_container
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.shared.application.email import EmailMessage, EmailRejected
from fragancia_api.shared.infrastructure.smtp_email_sender import SmtpEmailSender
from fragancia_api.shared.kernel import new_id
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.identity.conftest import NOW, store_user
from tests.integration.inventory.test_stock_status_flow import lose, outbox_rows, restock

pytestmark = pytest.mark.integration

TIMEOUT = 30


class StubSmtp:
    """Accepts mail for every recipient except those in `refuse` (address -> reply line)."""

    def __init__(self, refuse: dict[str, str]) -> None:
        self.refuse = refuse
        self.delivered: list[str] = []
        self.refused_attempts: list[str] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def reply(line: str) -> None:
            writer.write(f"{line}\r\n".encode())
            await writer.drain()

        accepted: list[str] = []
        try:
            await reply("220 stub ESMTP")
            while line := (await reader.readline()).decode().strip():
                verb = line.split(" ", 1)[0].upper()
                if verb == "RCPT":
                    address = line.split("<", 1)[1].split(">", 1)[0]
                    if address in self.refuse:
                        self.refused_attempts.append(address)
                        await reply(self.refuse[address])
                    else:
                        accepted.append(address)
                        await reply("250 2.1.5 ok")
                elif verb == "DATA":
                    await reply("354 end with <CRLF>.<CRLF>")
                    while (await reader.readline()).strip() != b".":
                        pass
                    self.delivered.extend(accepted)
                    accepted = []
                    await reply("250 2.0.0 queued")
                elif verb == "QUIT":
                    await reply("221 bye")
                    break
                else:
                    await reply("250 ok")
        finally:
            writer.close()


@pytest.fixture
async def smtp_stub(request: pytest.FixtureRequest) -> AsyncIterator[StubSmtp]:
    stub = StubSmtp(getattr(request, "param", {}))
    await stub.start()
    yield stub
    await stub.stop()


def sender_to(stub: StubSmtp) -> SmtpEmailSender:
    return SmtpEmailSender(
        host="127.0.0.1",
        port=stub.port,
        username=None,
        password=None,
        starttls=False,
        sender="no-reply@low-stock.test",
    )


MAIL = EmailMessage(to="ana@low-stock.test", subject="Hola", body="Hola\n")


# --- the adapter against real SMTP replies -----------------------------------------------------


@pytest.mark.parametrize(
    "smtp_stub", [{"ana@low-stock.test": "550 5.1.1 no such mailbox"}], indirect=True
)
async def test_a_550_reply_to_the_recipient_is_raised_as_email_rejected(
    smtp_stub: StubSmtp,
) -> None:
    with pytest.raises(EmailRejected, match="no such mailbox"):
        await asyncio.wait_for(sender_to(smtp_stub).send(MAIL), timeout=TIMEOUT)

    assert smtp_stub.refused_attempts == ["ana@low-stock.test"]
    assert smtp_stub.delivered == []


async def test_an_accepted_recipient_is_delivered_without_error(smtp_stub: StubSmtp) -> None:
    await asyncio.wait_for(sender_to(smtp_stub).send(MAIL), timeout=TIMEOUT)

    assert smtp_stub.delivered == ["ana@low-stock.test"]


@pytest.mark.parametrize(
    "smtp_stub", [{"ana@low-stock.test": "421 4.3.2 shutting down"}], indirect=True
)
async def test_a_421_reply_before_the_recipient_is_accepted_is_also_refused_by_smtplib(
    smtp_stub: StubSmtp,
) -> None:
    """Observation, confirmed locally: `smtplib` raises `SMTPRecipientsRefused` when every
    recipient gets a non-2xx reply, 4xx included, so the adapter treats a temporary refusal of
    the only recipient as a permanent one (the spec maps `SMTPRecipientsRefused` as a whole)."""
    with pytest.raises(EmailRejected):
        await asyncio.wait_for(sender_to(smtp_stub).send(MAIL), timeout=TIMEOUT)


async def test_a_refused_connection_is_not_a_rejection_so_it_is_retried() -> None:
    sender = SmtpEmailSender(
        host="127.0.0.1",
        port=1,
        username=None,
        password=None,
        starttls=False,
        sender="no-reply@low-stock.test",
    )

    with pytest.raises(ConnectionRefusedError):
        await asyncio.wait_for(sender.send(MAIL), timeout=TIMEOUT)


# --- the whole flow: outbox -> relay -> handler -> refusing server -----------------------------


async def add_owner(container: Container, name: str, address: str) -> UUID:
    user = User.create(Email(address), DisplayName(name), Role.OWNER, "h", now=NOW)
    await store_user(container, user)
    return user.id


async def test_a_refused_owner_is_skipped_and_logged_the_others_are_mailed_and_the_event_is_done(
    container: Container, settings: Settings, catalog_rows: CatalogRows
) -> None:
    suffix = new_id().hex[:10]
    ana = f"ana-{suffix}@low-stock.test"
    zoe = f"zoe-{suffix}@low-stock.test"
    stub = StubSmtp({ana: "550 5.1.1 no such mailbox"})
    await stub.start()
    try:
        await add_owner(container, "Ana", ana)
        await add_owner(container, "Zoe", zoe)
        _, [presentation_id] = await catalog_rows.perfume()
        await restock(container, presentation_id, 5)
        await lose(container, presentation_id, 2)
        refusing = build_container(
            settings.model_copy(update={"smtp_host": "127.0.0.1", "smtp_port": stub.port})
        )
        try:
            with structlog.testing.capture_logs() as logs:
                while await asyncio.wait_for(refusing.relay.relay_batch(), timeout=TIMEOUT):
                    pass
        finally:
            await refusing.close()

        assert stub.refused_attempts == [ana]
        assert stub.delivered == [zoe]
        [log] = [e for e in logs if e["event"] == "inventory.low_stock_email_rejected"]
        assert log["address"] == ana
        went_low = [r for r in await outbox_rows(container) if r["name"].endswith("went_low")]
        [row] = went_low
        assert row["published_at"] is not None  # consumed, not retried
        assert row["attempts"] == 0
    finally:
        await stub.stop()
