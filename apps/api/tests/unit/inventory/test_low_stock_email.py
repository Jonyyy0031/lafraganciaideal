"""Plan 004, application: `SendLowStockEmail` (the outbox subscriber) and the `IdentityOwners`
adapter that feeds it, with in-memory ports and the recording email sender."""

from uuid import UUID

import pytest
import structlog.testing

from fragancia_api.modules.identity import OwnerContact, OwnerDirectory
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.in_memory import InMemoryUsers
from fragancia_api.modules.inventory.application.handlers.low_stock_email import (
    LOW_STOCK_BODY,
    LOW_STOCK_SUBJECT,
    SendLowStockEmail,
)
from fragancia_api.modules.inventory.application.ports import Recipient
from fragancia_api.modules.inventory.domain.stock import StockWentLow
from fragancia_api.modules.inventory.infrastructure.identity_owners import IdentityOwners
from fragancia_api.shared.application.email import EmailMessage, EmailRejected
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.in_memory import FixedClock, RecordingEmailSender
from tests.unit.inventory.support import STOCKED, STOCKED_2, UNKNOWN, make_catalog

NOW = FixedClock().now()


class Owners:
    """`OwnerRecipients` over a fixed list."""

    def __init__(self, *recipients: Recipient) -> None:
        self._recipients = list(recipients)

    async def active_owners(self) -> list[Recipient]:
        return list(self._recipients)


def message(*lines: tuple[UUID, int, int]) -> EventMessage:
    event = StockWentLow(
        lines=tuple(
            {"presentation_id": str(pid), "on_hand": on_hand, "threshold": threshold}
            for pid, on_hand, threshold in lines
        )
    )
    return EventMessage(
        id=event.event_id, name=event.name, payload=event.payload(), occurred_at=event.occurred_at
    )


def handler(owners: Owners, email: RecordingEmailSender) -> SendLowStockEmail:
    return SendLowStockEmail(catalog=make_catalog(), owners=owners, email=email)


ANA = Recipient(name="Ana", email="ana@example.test")
LUIS = Recipient(name="Luis", email="luis@example.test")


def test_the_subject_and_body_template_are_the_plans_copy() -> None:
    assert LOW_STOCK_SUBJECT == "Stock bajo en La Fragancia Ideal"
    assert LOW_STOCK_BODY == (
        "Hola {name}:\n\n"
        "Estas presentaciones llegaron a su nivel de stock bajo:\n\n"
        "{lines}\n\n"
        "Revisa el inventario en el panel para resurtirlas.\n"
    )


async def test_one_email_goes_to_each_active_owner_with_their_own_greeting() -> None:
    email = RecordingEmailSender()

    await handler(Owners(ANA, LUIS), email)(message((STOCKED, 3, 3)))

    assert [m.to for m in email.sent] == ["ana@example.test", "luis@example.test"]
    assert {m.subject for m in email.sent} == {"Stock bajo en La Fragancia Ideal"}
    assert email.sent[0].body.startswith("Hola Ana:\n")
    assert email.sent[1].body.startswith("Hola Luis:\n")


async def test_the_body_lists_each_presentation_with_its_label_units_and_threshold() -> None:
    email = RecordingEmailSender()

    await handler(Owners(ANA), email)(message((STOCKED, 3, 3), (STOCKED_2, 0, 8)))

    [mail] = email.sent
    assert mail.body == (
        "Hola Ana:\n\n"
        "Estas presentaciones llegaron a su nivel de stock bajo:\n\n"
        "- Versace Eros EDT 50 ml: 3 en almacén (umbral 3)\n"
        "- Versace Eros EDT 100 ml: 0 en almacén (umbral 8)\n\n"
        "Revisa el inventario en el panel para resurtirlas.\n"
    )


async def test_an_unknown_presentation_is_listed_by_its_id() -> None:
    email = RecordingEmailSender()

    await handler(Owners(ANA), email)(message((UNKNOWN, 1, 3)))

    [mail] = email.sent
    assert f"- {UNKNOWN}: 1 en almacén (umbral 3)" in mail.body


async def test_with_no_active_owner_nothing_is_sent_and_it_is_logged() -> None:
    email = RecordingEmailSender()

    with structlog.testing.capture_logs() as logs:
        await handler(Owners(), email)(message((STOCKED, 3, 3), (STOCKED_2, 0, 3)))

    assert email.sent == []
    [log] = [entry for entry in logs if entry["event"] == "inventory.low_stock_no_owner"]
    assert log["presentations"] == 2


async def test_a_send_failure_raises_so_the_relay_retries() -> None:
    email = RecordingEmailSender()
    email.fail = True

    with pytest.raises(ConnectionError):
        await handler(Owners(ANA), email)(message((STOCKED, 3, 3)))


async def test_a_failure_on_a_later_owner_raises_after_the_earlier_ones_were_sent() -> None:
    """At-least-once: the relay retries the whole event, so Ana can get it twice."""

    class FailsOnSecond(RecordingEmailSender):
        async def send(self, message_: object) -> None:
            if self.sent:
                raise ConnectionError("smtp down")
            await super().send(message_)  # type: ignore[arg-type]

    email = FailsOnSecond()

    with pytest.raises(ConnectionError):
        await handler(Owners(ANA, LUIS), email)(message((STOCKED, 3, 3)))

    assert [m.to for m in email.sent] == ["ana@example.test"]


class RejectsAddresses(RecordingEmailSender):
    """Permanently refuses the listed addresses, as an SMTP 5xx reply would."""

    def __init__(self, *rejected: str, down: set[str] | None = None) -> None:
        super().__init__()
        self.rejected = set(rejected)
        self.down = down or set()
        self.attempted: list[str] = []

    async def send(self, message_: EmailMessage) -> None:
        self.attempted.append(message_.to)
        if message_.to in self.rejected:
            raise EmailRejected("550 mailbox unavailable")
        if message_.to in self.down:
            raise ConnectionError("smtp down")
        await super().send(message_)


async def test_a_rejected_owner_is_skipped_and_logged_and_the_others_still_get_it() -> None:
    email = RejectsAddresses("luis@example.test")
    zoe = Recipient(name="Zoe", email="zoe@example.test")

    with structlog.testing.capture_logs() as logs:
        await handler(Owners(ANA, LUIS, zoe), email)(message((STOCKED, 3, 3)))

    assert email.attempted == ["ana@example.test", "luis@example.test", "zoe@example.test"]
    assert [m.to for m in email.sent] == ["ana@example.test", "zoe@example.test"]
    [log] = [entry for entry in logs if entry["event"] == "inventory.low_stock_email_rejected"]
    assert log["address"] == "luis@example.test"


async def test_a_rejected_first_owner_does_not_stop_the_next() -> None:
    email = RejectsAddresses("ana@example.test")

    await handler(Owners(ANA, LUIS), email)(message((STOCKED, 3, 3)))

    assert [m.to for m in email.sent] == ["luis@example.test"]


async def test_when_every_owner_is_rejected_the_handler_still_returns_without_raising() -> None:
    email = RejectsAddresses("ana@example.test", "luis@example.test")

    with structlog.testing.capture_logs() as logs:
        await handler(Owners(ANA, LUIS), email)(message((STOCKED, 3, 3)))

    assert email.sent == []
    rejected = [entry for entry in logs if entry["event"] == "inventory.low_stock_email_rejected"]
    assert [entry["address"] for entry in rejected] == ["ana@example.test", "luis@example.test"]


async def test_a_non_rejection_error_still_raises_after_a_rejected_owner_was_skipped() -> None:
    zoe = Recipient(name="Zoe", email="zoe@example.test")
    email = RejectsAddresses("ana@example.test", down={"luis@example.test"})

    with pytest.raises(ConnectionError, match="smtp down"):
        await handler(Owners(ANA, LUIS, zoe), email)(message((STOCKED, 3, 3)))

    assert email.attempted == ["ana@example.test", "luis@example.test"]
    assert email.sent == []


async def test_an_event_without_lines_sends_nothing() -> None:
    email = RecordingEmailSender()
    empty = EventMessage(
        id=message().id, name=StockWentLow.name, payload={"lines": []}, occurred_at=NOW
    )

    await handler(Owners(ANA), email)(empty)

    assert email.sent == []


async def test_the_handler_reads_the_payload_the_event_really_produces() -> None:
    email = RecordingEmailSender()
    event = StockWentLow(lines=({"presentation_id": str(STOCKED), "on_hand": 2, "threshold": 3},))
    delivered = EventMessage(
        id=event.event_id, name=event.name, payload=event.payload(), occurred_at=NOW
    )

    await handler(Owners(ANA), email)(delivered)

    assert "- Versace Eros EDT 50 ml: 2 en almacén (umbral 3)" in email.sent[0].body


@pytest.mark.parametrize(
    "payload",
    [{}, {"lines": [{"presentation_id": "not-a-uuid", "on_hand": 1, "threshold": 3}]}],
    ids=["missing lines", "bad id"],
)
async def test_a_malformed_payload_raises_and_sends_nothing(payload: dict[str, object]) -> None:
    email = RecordingEmailSender()
    bad = EventMessage(id=message().id, name=StockWentLow.name, payload=payload, occurred_at=NOW)

    with pytest.raises((KeyError, ValueError)):
        await handler(Owners(ANA), email)(bad)

    assert email.sent == []


# --- IdentityOwners ----------------------------------------------------------------------------


async def _user(users: InMemoryUsers, email: str, name: str, role: Role, *, active: bool) -> None:
    user = User.create(Email(email), DisplayName(name), role, "hash", now=NOW)
    if not active:
        user.deactivate()
    await users.save(user)


async def test_the_adapter_maps_the_facades_contacts_to_inventorys_recipients() -> None:
    users = InMemoryUsers()
    await _user(users, "ana@example.test", "Ana", Role.OWNER, active=True)
    await _user(users, "staff@example.test", "Staff", Role.STAFF, active=True)
    owners = IdentityOwners(OwnerDirectory(users))

    assert await owners.active_owners() == [Recipient(name="Ana", email="ana@example.test")]
    assert (
        OwnerContact(name="Ana", email="ana@example.test")
        in await OwnerDirectory(users).active_owners()
    )
