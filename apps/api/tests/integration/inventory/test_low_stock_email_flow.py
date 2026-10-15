"""Plan 004, the whole email flow on real services: a stock command writes the outbox row, the
relay runs `SendLowStockEmail`, which reads the owners through the identity facade and the labels
through the catalog and sends over the real `SmtpEmailSender` to Mailpit (`SMTP_HOST` /
`SMTP_PORT`, as in CI). The messages are read back from Mailpit's HTTP API.

Every address is unique to its test and its Mailpit messages and identity rows are removed
afterwards. Mailpit unreachable -> NOT CONFIRMED (skipped). Every relay call is bounded by
`RELAY_TIMEOUT`, so an SMTP hang fails the test instead of the suite."""

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import structlog.testing
from sqlalchemy import select, text

from fragancia_api.config import Settings
from fragancia_api.container import Container, build_container
from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.sql_user_repository import SqlUserRepository
from fragancia_api.modules.inventory.application.commands.stock import RecordImportedCounts
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.shared.infrastructure.outbox import outbox
from fragancia_api.shared.kernel import Ok, new_id
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.identity.conftest import (
    NOW,
    clean_identity,  # noqa: F401  (autouse fixture: no stray owners get this test's emails)
    in_transaction,
    store_user,
)
from tests.integration.inventory.test_stock_status_flow import (
    lose,
    outbox_rows,
    restock,
)

pytestmark = pytest.mark.integration

MAILPIT_UI = f"http://127.0.0.1:{os.environ.get('MAILPIT_UI_PORT', '8026')}"
SUBJECT = "Stock bajo en La Fragancia Ideal"
RELAY_TIMEOUT = 30


@pytest.fixture
async def mailpit() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=MAILPIT_UI, timeout=5) as client:
        try:
            await client.get("/api/v1/info")
        except httpx.TransportError:
            pytest.skip(
                f"NOT CONFIRMED: Mailpit is not reachable at {MAILPIT_UI} (run `just up`); the "
                "low-stock email flow needs a real SMTP server"
            )
        yield client


class Owners:
    """The users one test creates, with unique addresses; removes them and their mail after."""

    def __init__(self, container: Container, mailpit: httpx.AsyncClient) -> None:
        self._container = container
        self._mailpit = mailpit
        self._suffix = new_id().hex[:10]
        self.addresses: list[str] = []
        self._ids: list[UUID] = []

    async def add(self, name: str, role: Role = Role.OWNER, *, active: bool = True) -> str:
        address = f"{name.lower().replace(' ', '-')}-{self._suffix}@low-stock.test"
        user = User.create(Email(address), DisplayName(name), role, "h", now=NOW)
        await store_user(self._container, user)
        if not active:
            user.deactivate()
            repository = SqlUserRepository(self._container.database)
            await in_transaction(self._container, lambda: repository.save(user))
        self.addresses.append(address)
        self._ids.append(user.id)
        return address

    async def messages_to(self, address: str) -> list[dict[str, Any]]:
        response = await self._mailpit.get("/api/v1/search", params={"query": f"to:{address}"})
        found: list[dict[str, Any]] = response.json()["messages"]
        return found

    async def text_of(self, message: dict[str, Any]) -> str:
        detail = (await self._mailpit.get(f"/api/v1/message/{message['ID']}")).json()
        # SMTP carries CRLF line ends; the body the handler built uses LF.
        return str(detail["Text"]).replace("\r\n", "\n")

    async def cleanup(self) -> None:
        for address in self.addresses:
            ids = [m["ID"] for m in await self.messages_to(address)]
            if ids:  # only messages addressed to this test's own unique recipients
                await self._mailpit.request("DELETE", "/api/v1/messages", json={"IDs": ids})
        if self._ids:
            async with self._container.database.engine.begin() as connection:
                found = (
                    await connection.execute(
                        text("SELECT count(*) FROM identity.users WHERE id = ANY(:ids)"),
                        {"ids": self._ids},
                    )
                ).scalar_one()
                assert found == len(self._ids)  # proves the WHERE before the DELETE
                await connection.execute(
                    text("DELETE FROM identity.users WHERE id = ANY(:ids)"), {"ids": self._ids}
                )


@pytest.fixture
async def owners(container: Container, mailpit: httpx.AsyncClient) -> AsyncIterator[Owners]:
    world = Owners(container, mailpit)
    yield world
    await world.cleanup()


async def relay(container: Container) -> int:
    return await asyncio.wait_for(container.relay.relay_batch(), timeout=RELAY_TIMEOUT)


async def went_low_rows(container: Container) -> list[dict[str, Any]]:
    return [
        row for row in await outbox_rows(container) if row["name"] == "inventory.stock.went_low"
    ]


async def label_of(container: Container, presentation_id: UUID) -> str:
    catalog = CatalogPresentations(container.services.get(PresentationDirectory))
    described = await catalog.describe([presentation_id])
    return described[presentation_id].label


# --- the flow ----------------------------------------------------------------------------------


async def test_a_first_drop_emails_each_active_owner_once_and_nobody_else(
    container: Container, catalog_rows: CatalogRows, owners: Owners
) -> None:
    ana = await owners.add("Ana")
    zoe = await owners.add("Zoe")
    gone = await owners.add("Gone", active=False)
    staff = await owners.add("Staff", Role.STAFF)
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await relay(container)
    await lose(container, presentation_id, 2)
    [row] = await went_low_rows(container)
    assert row["published_at"] is None

    await relay(container)

    [mail_ana] = await owners.messages_to(ana)
    [mail_zoe] = await owners.messages_to(zoe)
    assert await owners.messages_to(gone) == []
    assert await owners.messages_to(staff) == []
    assert mail_ana["Subject"] == mail_zoe["Subject"] == SUBJECT
    label = await label_of(container, presentation_id)
    assert await owners.text_of(mail_ana) == (
        "Hola Ana:\n\n"
        "Estas presentaciones llegaron a su nivel de stock bajo:\n\n"
        f"- {label}: 3 en almacén (umbral 3)\n\n"
        "Revisa el inventario en el panel para resurtirlas.\n"
    )
    assert (await owners.text_of(mail_zoe)).startswith("Hola Zoe:\n")
    [relayed] = [r for r in await went_low_rows(container)]
    assert relayed["published_at"] is not None


async def test_no_repeat_while_low_and_one_more_email_after_it_re_arms(
    container: Container, catalog_rows: CatalogRows, owners: Owners
) -> None:
    ana = await owners.add("Ana")
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await lose(container, presentation_id, 2)
    await relay(container)
    assert len(await owners.messages_to(ana)) == 1

    await lose(container, presentation_id, 1)  # 2 on hand: still low
    await relay(container)
    assert len(await owners.messages_to(ana)) == 1

    await restock(container, presentation_id, 4)  # 6: re-armed
    await lose(container, presentation_id, 3)  # 3: low again
    await relay(container)
    assert len(await owners.messages_to(ana)) == 2


async def test_an_import_of_three_counts_at_zero_sends_one_email_listing_all_three(
    container: Container, catalog_rows: CatalogRows, owners: Owners
) -> None:
    ana = await owners.add("Ana")
    _, ids = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 75}, {"ml": 100}))
    counts = container.services.get(RecordImportedCounts)
    await counts.execute({presentation_id: 10 for presentation_id in ids}, "admin-load")
    await relay(container)
    assert await owners.messages_to(ana) == []

    result = await counts.execute({presentation_id: 0 for presentation_id in ids}, "admin-flow")
    await relay(container)

    assert isinstance(result, Ok)
    [mail] = await owners.messages_to(ana)
    body = await owners.text_of(mail)
    for presentation_id in ids:
        assert f"- {await label_of(container, presentation_id)}: 0 en almacén (umbral 3)" in body


async def test_with_no_active_owner_the_event_is_published_and_logged_without_email(
    container: Container, catalog_rows: CatalogRows, owners: Owners
) -> None:
    gone = await owners.add("Gone", active=False)
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await lose(container, presentation_id, 2)

    with structlog.testing.capture_logs() as logs:
        await relay(container)

    assert await owners.messages_to(gone) == []
    assert [log for log in logs if log["event"] == "inventory.low_stock_no_owner"] != []
    assert all(row["published_at"] is not None for row in await outbox_rows(container))


# --- failure and retry -------------------------------------------------------------------------


async def test_an_smtp_failure_keeps_the_event_for_a_retry_that_then_delivers(
    container: Container, settings: Settings, catalog_rows: CatalogRows, owners: Owners
) -> None:
    ana = await owners.add("Ana")
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await lose(container, presentation_id, 2)
    # Rows other than `went_low` are relayed without SMTP; port 1 refuses the connection at once.
    broken = build_container(settings.model_copy(update={"smtp_port": 1}))
    try:
        await asyncio.wait_for(broken.relay.relay_batch(), timeout=RELAY_TIMEOUT)
    finally:
        await broken.close()

    [row] = await went_low_rows(container)
    assert row["published_at"] is None
    assert row["attempts"] == 1
    assert row["last_error"]
    assert await owners.messages_to(ana) == []

    async with container.database.engine.begin() as connection:
        due = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM platform.outbox WHERE id = :id AND published_at IS NULL"
                ),
                {"id": row["id"]},
            )
        ).scalar_one()
        assert due == 1  # proves the WHERE before the UPDATE
        await connection.execute(
            text("UPDATE platform.outbox SET available_at = now() WHERE id = :id"),
            {"id": row["id"]},
        )
    await relay(container)

    assert len(await owners.messages_to(ana)) == 1
    async with container.database.reader() as session:
        stored = (
            await session.execute(select(outbox.c.published_at).where(outbox.c.id == row["id"]))
        ).scalar_one()
    assert stored is not None
