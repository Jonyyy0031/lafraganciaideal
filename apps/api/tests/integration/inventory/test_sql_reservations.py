"""Plan 003 on PostgreSQL: the schema constraints, the SQL repositories and queries, and the
flows through the real container (reserve, commit, release, extend, the expiry job, the event
reaching the catalog copy). The races are in `test_reservation_races.py`."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fragancia_api.container import Container
from fragancia_api.modules.inventory.application.commands.reservations import (
    CommitReservation,
    ExpireReservations,
    ExtendReservation,
    ReleaseReservation,
    ReserveStock,
)
from fragancia_api.modules.inventory.application.queries.stock import GetStock, ListStock
from fragancia_api.modules.inventory.application.stock_reservations import StockReservations
from fragancia_api.modules.inventory.domain.reservation import (
    Reservation,
    ReservationStatus,
)
from fragancia_api.modules.inventory.infrastructure.sql_reservation_repository import (
    SqlReservationRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_queries import SqlStockQueries
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import (
    item_row,
    make_due,
    reservation_row,
    reserve_ok,
    sale_movements,
    table_count,
    value_of,
)
from tests.integration.inventory.test_stock_settings_race import (
    TIMEOUT,
    Scenario,
    is_blocked,
    run_in_transaction,
)
from tests.integration.inventory.test_stock_status_flow import (
    MADE_TO_ORDER,
    copy_of,
    lose,
    outbox_rows,
    relay_all,
    restock,
    statuses,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def new_reservation(
    quantities: dict[UUID, int],
    reference: str = "REF-1",
    *,
    expires_at: datetime = NOW + timedelta(minutes=2),
) -> Reservation:
    return value_of(Reservation.create(reference, quantities, expires_at, NOW))


async def add(container: Container, reservation: Reservation) -> bool:
    repo = SqlReservationRepository(container.database)

    async def work() -> bool:
        return await repo.add(reservation)

    return await run_in_transaction(container, work)


async def insert_raw_reservation(
    container: Container, reference: str, status: str = "active"
) -> UUID:
    reservation_id = new_id()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO inventory.reservations "
                "(id, reference, status, expires_at, created_at) "
                "VALUES (:id, :ref, :status, :at, :at)"
            ),
            {"id": reservation_id, "ref": reference, "status": status, "at": NOW},
        )
    return reservation_id


# --- schema ------------------------------------------------------------------------------------


async def test_reserved_defaults_to_zero_when_a_row_is_inserted_without_it(
    container: Container,
) -> None:
    presentation_id = new_id()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO inventory.stock_items (presentation_id, on_hand, updated_at) "
                "VALUES (:p, 4, now())"
            ),
            {"p": presentation_id},
        )

    assert await item_row(container, presentation_id) == (4, 0)


async def test_the_database_refuses_a_negative_reserved(container: Container) -> None:
    with pytest.raises(IntegrityError, match="ck_stock_items_reserved_not_negative"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO inventory.stock_items "
                    "(presentation_id, on_hand, reserved, updated_at) VALUES (:p, 1, -1, now())"
                ),
                {"p": new_id()},
            )
    assert await table_count(container, "stock_items") == 0


async def test_an_update_to_a_negative_reserved_is_refused_too(container: Container) -> None:
    presentation_id = new_id()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO inventory.stock_items "
                "(presentation_id, on_hand, reserved, updated_at) "
                "VALUES (:p, 2, 1, now())"
            ),
            {"p": presentation_id},
        )

    with pytest.raises(IntegrityError, match="ck_stock_items_reserved_not_negative"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text("UPDATE inventory.stock_items SET reserved = -1 WHERE presentation_id = :p"),
                {"p": presentation_id},
            )
    assert await item_row(container, presentation_id) == (2, 1)


async def test_a_reserved_above_on_hand_is_allowed_by_the_database(container: Container) -> None:
    """A loss may leave `on_hand` below `reserved` (decision 28): no constraint relates them."""
    presentation_id = new_id()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO inventory.stock_items "
                "(presentation_id, on_hand, reserved, updated_at) "
                "VALUES (:p, 1, 5, now())"
            ),
            {"p": presentation_id},
        )

    assert await item_row(container, presentation_id) == (1, 5)


async def test_the_database_refuses_a_duplicated_reservation_reference(
    container: Container,
) -> None:
    await insert_raw_reservation(container, "DUP-1")

    with pytest.raises(IntegrityError, match="uq_reservations_reference"):
        await insert_raw_reservation(container, "DUP-1")
    assert await table_count(container, "reservations") == 1


async def test_the_database_refuses_a_line_with_a_quantity_of_zero(container: Container) -> None:
    reservation_id = await insert_raw_reservation(container, "Q-0")

    with pytest.raises(IntegrityError, match="ck_reservation_lines_quantity_positive"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO inventory.reservation_lines "
                    "(reservation_id, presentation_id, quantity) VALUES (:r, :p, 0)"
                ),
                {"r": reservation_id, "p": new_id()},
            )
    assert await table_count(container, "reservation_lines") == 0


async def test_a_line_needs_an_existing_reservation(container: Container) -> None:
    with pytest.raises(IntegrityError, match="fk_reservation_lines_reservation_id_reservations"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO inventory.reservation_lines "
                    "(reservation_id, presentation_id, quantity) VALUES (:r, :p, 1)"
                ),
                {"r": new_id(), "p": new_id()},
            )


async def test_a_reservation_holds_one_line_per_presentation(container: Container) -> None:
    reservation_id = await insert_raw_reservation(container, "PK-1")
    presentation_id = new_id()
    insert = text(
        "INSERT INTO inventory.reservation_lines (reservation_id, presentation_id, quantity) "
        "VALUES (:r, :p, 1)"
    )
    async with container.database.engine.begin() as connection:
        await connection.execute(insert, {"r": reservation_id, "p": presentation_id})

    with pytest.raises(IntegrityError, match="pk_reservation_lines"):
        async with container.database.engine.begin() as connection:
            await connection.execute(insert, {"r": reservation_id, "p": presentation_id})


async def test_a_line_may_name_a_presentation_the_inventory_schema_does_not_own(
    container: Container,
) -> None:
    """No cross-module foreign key: the presentation id is a plain column."""
    reservation_id = await insert_raw_reservation(container, "NOFK-1")
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO inventory.reservation_lines "
                "(reservation_id, presentation_id, quantity) VALUES (:r, :p, 1)"
            ),
            {"r": reservation_id, "p": new_id()},
        )

    assert await table_count(container, "reservation_lines") == 1


async def test_the_expiry_scan_has_a_partial_index_on_active_reservations(
    container: Container,
) -> None:
    async with container.database.engine.connect() as connection:
        definition = (
            await connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'inventory' "
                    "AND indexname = 'ix_reservations_active_expires'"
                )
            )
        ).scalar_one()

    assert "(expires_at)" in definition
    assert "status" in definition and "active" in definition  # WHERE status = 'active'


# --- SqlReservationRepository ------------------------------------------------------------------


async def test_add_stores_the_reservation_and_its_lines_and_loads_them_back(
    container: Container,
) -> None:
    a, b = UUID(int=1), UUID(int=2)
    reservation = new_reservation({b: 3, a: 2})

    assert await add(container, reservation) is True

    repo = SqlReservationRepository(container.database)

    async def load() -> Reservation | None:
        return await repo.get_for_update(reservation.id)

    loaded = await run_in_transaction(container, load)
    assert loaded is not None
    assert (loaded.id, loaded.reference, loaded.status) == (
        reservation.id,
        "REF-1",
        ReservationStatus.ACTIVE,
    )
    assert (loaded.expires_at, loaded.created_at, loaded.closed_at) == (
        reservation.expires_at,
        NOW,
        None,
    )
    assert [(line.presentation_id, line.quantity) for line in loaded.lines] == [(a, 2), (b, 3)]
    assert await table_count(container, "reservation_lines") == 2


async def test_add_returns_false_for_a_taken_reference_and_writes_nothing(
    container: Container,
) -> None:
    await add(container, new_reservation({UUID(int=1): 1}, "TAKEN"))
    before = (
        await table_count(container, "reservations"),
        await table_count(container, "reservation_lines"),
    )

    again = await add(container, new_reservation({UUID(int=2): 5, UUID(int=3): 1}, "TAKEN"))

    assert again is False
    assert (
        await table_count(container, "reservations"),
        await table_count(container, "reservation_lines"),
    ) == before


async def test_a_reservation_added_in_a_transaction_that_fails_is_not_stored(
    container: Container,
) -> None:
    repo = SqlReservationRepository(container.database)
    reservation = new_reservation({UUID(int=1): 1})

    async def work() -> Result[None, DomainError]:
        assert await repo.add(reservation)
        return Err(DomainError(code="X", message="roll back"))

    result = await SqlTransactionRunner(container.database).run(work)

    assert isinstance(result, Err)
    assert await table_count(container, "reservations") == 0
    assert await table_count(container, "reservation_lines") == 0


async def test_get_for_update_of_an_unknown_reservation_is_none(container: Container) -> None:
    repo = SqlReservationRepository(container.database)

    async def load() -> Reservation | None:
        return await repo.get_for_update(new_id())

    assert await run_in_transaction(container, load) is None


async def test_save_updates_status_expiry_and_closed_at_but_never_the_lines(
    container: Container,
) -> None:
    reservation = new_reservation({UUID(int=1): 2})
    await add(container, reservation)
    repo = SqlReservationRepository(container.database)
    new_expiry = NOW + timedelta(hours=72)

    async def work() -> None:
        loaded = await repo.get_for_update(reservation.id)
        assert loaded is not None
        assert isinstance(loaded.extend(new_expiry, NOW), Ok)
        loaded.commit(NOW + timedelta(seconds=30))
        await repo.save(loaded)

    await run_in_transaction(container, work)

    row = await reservation_row(container, reservation.id)
    assert (row["status"], row["expires_at"], row["closed_at"]) == (
        "committed",
        new_expiry,
        NOW + timedelta(seconds=30),
    )
    assert row["reference"] == "REF-1"
    assert await table_count(container, "reservation_lines") == 1


async def test_get_for_update_locks_the_reservation_row(container: Container) -> None:
    reservation = new_reservation({UUID(int=1): 1})
    await add(container, reservation)
    repo = SqlReservationRepository(container.database)

    async def lock() -> None:
        await repo.get_for_update(reservation.id)

    async with Scenario(container) as scenario:
        _, release = await scenario.hold(lock)
        waiting = scenario.spawn(run_in_transaction(container, lock))

        assert await is_blocked(waiting)
        release.set()
        await scenario.finish(waiting)


async def due_in(container: Container, minutes: int, reference: str, ref_id: int) -> Reservation:
    """A stored active reservation expiring `minutes` after NOW, with a chosen id."""
    reservation = new_reservation(
        {UUID(int=900 + ref_id): 1}, reference, expires_at=NOW + timedelta(minutes=minutes)
    )
    reservation.id = UUID(int=ref_id)
    await add(container, reservation)
    return reservation


async def test_claim_expired_returns_only_active_due_reservations_ordered_by_id_with_lines(
    container: Container,
) -> None:
    repo = SqlReservationRepository(container.database)
    third = await due_in(container, 1, "R-3", 3)
    first = await due_in(container, 1, "R-1", 1)
    await due_in(container, 5, "R-NOT-DUE", 4)
    closed = await due_in(container, 1, "R-CLOSED", 2)
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE inventory.reservations SET status = 'released' WHERE id = :id"),
            {"id": closed.id},
        )

    async def claim() -> list[Reservation]:
        return await repo.claim_expired(NOW + timedelta(minutes=2), 100)

    claimed = await run_in_transaction(container, claim)

    assert [r.id for r in claimed] == [first.id, third.id]
    assert [[(line.presentation_id, line.quantity) for line in r.lines] for r in claimed] == [
        [(UUID(int=901), 1)],
        [(UUID(int=903), 1)],
    ]


async def test_claim_expired_counts_the_exact_expiry_instant_as_due(container: Container) -> None:
    repo = SqlReservationRepository(container.database)
    exact = await due_in(container, 2, "R-EXACT", 1)

    async def claim_at(moment: datetime) -> list[Reservation]:
        return await repo.claim_expired(moment, 100)

    before = await run_in_transaction(
        container, lambda: claim_at(exact.expires_at - timedelta(microseconds=1))
    )
    at = await run_in_transaction(container, lambda: claim_at(exact.expires_at))

    assert before == []
    assert [r.id for r in at] == [exact.id]


async def test_claim_expired_honors_the_limit_taking_the_lowest_ids(container: Container) -> None:
    repo = SqlReservationRepository(container.database)
    for n in (3, 1, 2):
        await due_in(container, 1, f"R-{n}", n)

    async def claim() -> list[Reservation]:
        return await repo.claim_expired(NOW + timedelta(minutes=5), 2)

    claimed = await run_in_transaction(container, claim)

    assert [r.id for r in claimed] == [UUID(int=1), UUID(int=2)]


async def test_claim_expired_skips_a_reservation_another_transaction_has_locked(
    container: Container,
) -> None:
    repo = SqlReservationRepository(container.database)
    locked = await due_in(container, 1, "R-LOCKED", 1)
    free = await due_in(container, 1, "R-FREE", 2)

    async def lock_first() -> None:
        assert await repo.get_for_update(locked.id) is not None

    async def claim() -> list[Reservation]:
        return await repo.claim_expired(NOW + timedelta(minutes=5), 100)

    async with Scenario(container) as scenario:
        _, release = await scenario.hold(lock_first)

        claimed = await asyncio.wait_for(run_in_transaction(container, claim), timeout=TIMEOUT)

        assert [r.id for r in claimed] == [free.id]  # no wait, no lock on the held one
        release.set()


# --- SqlStockRepository: reserved and get_many_for_update --------------------------------------


async def test_save_and_load_round_trip_reserved(container: Container) -> None:
    repo = SqlStockRepository(container.database)
    presentation_id = new_id()

    async def write() -> None:
        item = await repo.get_for_update(presentation_id, now=NOW)
        assert item is not None
        item.on_hand = 5
        assert isinstance(item.hold(2), Ok)
        await repo.save(item)

    async def read() -> tuple[int, int]:
        item = await repo.get_for_update(presentation_id, now=NOW)
        assert item is not None
        return item.on_hand, item.reserved

    await run_in_transaction(container, write)

    assert await run_in_transaction(container, read) == (5, 2)
    assert await item_row(container, presentation_id) == (5, 2)


async def test_get_many_for_update_inserts_the_missing_items_empty_and_returns_all_ascending(
    container: Container,
) -> None:
    repo = SqlStockRepository(container.database)
    ids = [UUID(int=30), UUID(int=10), UUID(int=20)]

    async def load() -> dict[UUID, object]:
        found = await repo.get_many_for_update(ids + [UUID(int=10)], now=NOW)  # a repeated id
        return dict(found)

    found = await run_in_transaction(container, load)

    assert list(found) == [UUID(int=10), UUID(int=20), UUID(int=30)]
    assert await table_count(container, "stock_items") == 3
    assert await item_row(container, UUID(int=20)) == (0, 0)


async def test_get_many_for_update_keeps_the_stored_values_of_existing_items(
    container: Container,
) -> None:
    repo = SqlStockRepository(container.database)
    existing = UUID(int=2)

    async def seed() -> None:
        item = await repo.get_for_update(existing, now=NOW)
        assert item is not None
        item.on_hand = 7
        item.hold(3)
        await repo.save(item)

    await run_in_transaction(container, seed)

    async def load() -> tuple[int, int, int]:
        found = await repo.get_many_for_update([UUID(int=1), existing], now=NOW)
        return found[existing].on_hand, found[existing].reserved, found[UUID(int=1)].on_hand

    assert await run_in_transaction(container, load) == (7, 3, 0)


async def test_get_many_for_update_of_nothing_is_empty(container: Container) -> None:
    repo = SqlStockRepository(container.database)

    async def load() -> dict[UUID, object]:
        return dict(await repo.get_many_for_update([], now=NOW))

    assert await run_in_transaction(container, load) == {}
    assert await table_count(container, "stock_items") == 0


async def test_get_many_for_update_locks_every_item_it_returns(container: Container) -> None:
    repo = SqlStockRepository(container.database)
    ids = [UUID(int=1), UUID(int=2)]

    async def lock_both() -> None:
        await repo.get_many_for_update(ids, now=NOW)

    async def lock_second() -> None:
        await repo.get_for_update(ids[1], now=NOW)

    async with Scenario(container) as scenario:
        _, release = await scenario.hold(lock_both)
        waiting = scenario.spawn(run_in_transaction(container, lock_second))

        assert await is_blocked(waiting)
        release.set()
        await scenario.finish(waiting)


# --- SqlStockQueries ---------------------------------------------------------------------------


async def test_queries_return_reserved_next_to_on_hand_and_override(container: Container) -> None:
    repo = SqlStockRepository(container.database)
    counted, never = new_id(), new_id()

    async def write() -> None:
        item = await repo.get_for_update(counted, now=NOW)
        assert item is not None
        item.on_hand = 6
        item.hold(2)
        await repo.save(item)

    await run_in_transaction(container, write)
    queries = SqlStockQueries(container.database)

    assert await queries.get(counted) == (6, 2, None)
    assert await queries.get(never) is None
    assert await queries.on_hand_by_id([counted, never]) == {counted: (6, 2, None)}


async def seed_hold(
    container: Container,
    presentation_id: UUID,
    reference: str,
    quantity: int,
    minutes: int,
    rid: int,
) -> UUID:
    reservation = new_reservation(
        {presentation_id: quantity}, reference, expires_at=NOW + timedelta(minutes=minutes)
    )
    reservation.id = UUID(int=rid)
    await add(container, reservation)
    return reservation.id


async def test_active_holds_are_soonest_expiry_first_ties_by_id_and_only_active(
    container: Container,
) -> None:
    presentation_id, other = new_id(), new_id()
    late = await seed_hold(container, presentation_id, "H-LATE", 1, 60, 5)
    tie_high = await seed_hold(container, presentation_id, "H-TIE-HIGH", 2, 10, 4)
    tie_low = await seed_hold(container, presentation_id, "H-TIE-LOW", 3, 10, 3)
    soon = await seed_hold(container, presentation_id, "H-SOON", 4, 1, 9)
    released = await seed_hold(container, presentation_id, "H-REL", 1, 5, 1)
    await seed_hold(container, other, "H-OTHER", 1, 1, 2)
    async with container.database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE inventory.reservations SET status = 'released' WHERE id = :id"),
            {"id": released},
        )

    page = await SqlStockQueries(container.database).active_holds(presentation_id, page=1, size=10)

    assert [h.reservation_id for h in page.items] == [soon, tie_low, tie_high, late]
    assert [(h.reference, h.quantity) for h in page.items] == [
        ("H-SOON", 4),
        ("H-TIE-LOW", 3),
        ("H-TIE-HIGH", 2),
        ("H-LATE", 1),
    ]
    assert page.total == 4
    assert page.items[0].created_at == NOW
    assert page.items[0].expires_at == NOW + timedelta(minutes=1)


async def test_active_holds_paginate(container: Container) -> None:
    presentation_id = new_id()
    ids = [await seed_hold(container, presentation_id, f"P-{n}", 1, n + 1, n + 1) for n in range(3)]
    queries = SqlStockQueries(container.database)

    first = await queries.active_holds(presentation_id, page=1, size=2)
    second = await queries.active_holds(presentation_id, page=2, size=2)
    beyond = await queries.active_holds(presentation_id, page=3, size=2)

    assert [h.reservation_id for h in first.items] == ids[:2]
    assert [h.reservation_id for h in second.items] == ids[2:]
    assert beyond.items == []
    assert (first.total, second.total, beyond.total) == (3, 3, 3)


async def test_active_holds_of_a_presentation_without_any_are_empty(container: Container) -> None:
    page = await SqlStockQueries(container.database).active_holds(new_id(), page=1, size=10)

    assert (page.items, page.total) == ([], 0)


# --- the flows through the real container ------------------------------------------------------


async def test_reserve_shows_in_the_stock_the_holds_and_the_catalog_copy(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await relay_all(container)

    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1", minutes=2)

    stock = value_of(await container.services.get(GetStock).execute(presentation_id))
    assert (stock.on_hand, stock.reserved, stock.available) == (5, 2, 3)
    holds = await SqlStockQueries(container.database).active_holds(presentation_id, page=1, size=10)
    assert [(h.reservation_id, h.reference, h.quantity) for h in holds.items] == [
        (reservation_id, "QA-1", 2)
    ]
    assert await sale_movements(container, presentation_id) == []
    await relay_all(container)
    available, threshold, _ = await copy_of(container, presentation_id)
    assert (available, threshold) == (3, 3)
    assert await statuses(container, perfume_id) == {100: "last_units"}  # 3 <= 3


async def test_reserve_puts_one_stock_changed_row_with_the_available_units_in_the_outbox(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    before = len(await outbox_rows(container))

    await reserve_ok(container, {presentation_id: 2}, "QA-1")

    rows = (await outbox_rows(container))[before:]
    [row] = rows
    assert row["name"] == "inventory.stock.changed"
    assert (row["payload"]["available"], row["payload"]["threshold"]) == (3, 3)


async def test_a_reservation_that_empties_the_shelf_sells_out_the_storefront(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 2)

    await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await relay_all(container)

    assert (await copy_of(container, presentation_id))[0] == 0
    assert await statuses(container, perfume_id) == {100: "sold_out"}


async def test_reserve_is_all_or_nothing_and_leaves_no_row_behind(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [a, b] = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 100}))
    await restock(container, a, 5)
    await restock(container, b, 3)
    await relay_all(container)
    outbox_before = len(await outbox_rows(container))

    result = await container.services.get(ReserveStock).execute(
        "QA-1", {a: 1, b: 10}, datetime.now(UTC) + timedelta(minutes=2)
    )

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert result.error.details == {
        "lines": [{"presentation_id": str(b), "requested": 10, "available": 3}]
    }
    assert await item_row(container, a) == (5, 0)
    assert await item_row(container, b) == (3, 0)
    assert await table_count(container, "reservations") == 0
    assert await table_count(container, "reservation_lines") == 0
    assert len(await outbox_rows(container)) == outbox_before


async def test_a_standalone_refused_reserve_does_not_leave_the_empty_item_it_locked(
    container: Container, catalog_rows: CatalogRows
) -> None:
    """Standalone only: the runner rolls back the whole transaction, including the empty stock row
    the lock inserted. Inside a caller's transaction that row remains (0 on hand, harmless)."""
    _, [presentation_id] = await catalog_rows.perfume()

    result = await container.services.get(ReserveStock).execute(
        "QA-1", {presentation_id: 1}, datetime.now(UTC) + timedelta(minutes=2)
    )

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert await table_count(container, "stock_items") == 0


async def test_reserving_with_a_reference_already_taken_changes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    await reserve_ok(container, {presentation_id: 2}, "QA-1")
    outbox_before = len(await outbox_rows(container))

    result = await container.services.get(ReserveStock).execute(
        "QA-1", {presentation_id: 1}, datetime.now(UTC) + timedelta(minutes=2)
    )

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_RESERVATION_REFERENCE_TAKEN"
    assert await item_row(container, presentation_id) == (5, 2)
    assert await table_count(container, "reservations") == 1
    assert len(await outbox_rows(container)) == outbox_before


async def test_reserving_a_made_to_order_or_unknown_presentation_is_refused(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [made_to_order] = await catalog_rows.perfume(sizes=(MADE_TO_ORDER,))
    expires = datetime.now(UTC) + timedelta(minutes=2)
    reserve = container.services.get(ReserveStock)

    made = await reserve.execute("QA-1", {made_to_order: 1}, expires)
    unknown = await reserve.execute("QA-2", {new_id(): 1}, expires)

    assert isinstance(made, Err)
    assert made.error.code == "INVENTORY_PRESENTATION_NOT_STOCKED"
    assert isinstance(unknown, Err)
    assert unknown.error.code == "INVENTORY_PRESENTATION_NOT_FOUND"
    assert await table_count(container, "reservations") == 0


async def test_extend_moves_the_stored_expiry_and_refuses_an_earlier_one(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1", minutes=2)
    extend = container.services.get(ExtendReservation)
    new_expiry = datetime.now(UTC) + timedelta(hours=72)

    assert isinstance(await extend.execute(reservation_id, new_expiry), Ok)
    earlier = await extend.execute(reservation_id, datetime.now(UTC) + timedelta(hours=1))

    assert (await reservation_row(container, reservation_id))["expires_at"] == new_expiry
    assert isinstance(earlier, Err)
    assert earlier.error.code == "INVENTORY_RESERVATION_EXPIRY_INVALID"


async def test_extend_an_expired_reservation_is_not_active(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await make_due(container, reservation_id)

    result = await container.services.get(ExtendReservation).execute(
        reservation_id, datetime.now(UTC) + timedelta(days=3)
    )

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_RESERVATION_NOT_ACTIVE"


async def test_commit_records_a_sale_movement_and_is_idempotent(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    commit = container.services.get(CommitReservation)

    first = await commit.execute(reservation_id)
    second = await commit.execute(reservation_id)

    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert await item_row(container, presentation_id) == (3, 0)
    assert await sale_movements(container, presentation_id) == [
        {
            "kind": "sale",
            "quantity": 2,
            "delta": -2,
            "on_hand_after": 3,
            "reason": "Venta: QA-1",
            "actor_id": "system",
        }
    ]
    row = await reservation_row(container, reservation_id)
    assert (row["status"], row["closed_at"] is not None) == ("committed", True)
    await relay_all(container)
    assert (await copy_of(container, presentation_id))[0] == 3
    assert await statuses(container, perfume_id) == {100: "last_units"}


async def test_release_returns_the_units_without_a_movement_and_is_idempotent(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    movements = await table_count(container, "stock_movements")
    release = container.services.get(ReleaseReservation)

    first = await release.execute(reservation_id)
    second = await release.execute(reservation_id)

    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert await item_row(container, presentation_id) == (5, 0)
    assert await table_count(container, "stock_movements") == movements
    assert (await reservation_row(container, reservation_id))["status"] == "released"
    await relay_all(container)
    assert (await copy_of(container, presentation_id))[0] == 5


async def test_releasing_a_committed_reservation_is_refused(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await container.services.get(CommitReservation).execute(reservation_id)

    result = await container.services.get(ReleaseReservation).execute(reservation_id)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_RESERVATION_COMMITTED"
    assert await item_row(container, presentation_id) == (3, 0)


async def test_the_expiry_job_expires_a_due_reservation_and_the_storefront_follows(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 2)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await relay_all(container)
    assert await statuses(container, perfume_id) == {100: "sold_out"}
    await make_due(container, reservation_id)

    expired = await container.services.get(ExpireReservations).run_once()

    assert expired == 1
    row = await reservation_row(container, reservation_id)
    assert (row["status"], row["closed_at"] is not None) == ("expired", True)
    assert await item_row(container, presentation_id) == (2, 0)
    await relay_all(container)
    assert (await copy_of(container, presentation_id))[0] == 2
    assert await statuses(container, perfume_id) == {100: "last_units"}
    assert await container.services.get(ExpireReservations).run_once() == 0


async def test_the_expiry_job_leaves_a_reservation_that_is_not_due(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1", minutes=30)

    assert await container.services.get(ExpireReservations).run_once() == 0

    assert (await reservation_row(container, reservation_id))["status"] == "active"
    assert await item_row(container, presentation_id) == (5, 2)


async def test_a_late_commit_after_the_job_sells_when_units_are_available(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await make_due(container, reservation_id)
    await container.services.get(ExpireReservations).run_once()

    result = await container.services.get(CommitReservation).execute(reservation_id)

    assert isinstance(result, Ok)
    assert await item_row(container, presentation_id) == (3, 0)
    assert (await reservation_row(container, reservation_id))["status"] == "committed"
    assert len(await sale_movements(container, presentation_id)) == 1


async def test_a_late_commit_with_no_units_available_is_refused_and_stays_expired(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 2)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await make_due(container, reservation_id)
    await container.services.get(ExpireReservations).run_once()
    await reserve_ok(container, {presentation_id: 2}, "QA-2")  # another order took them

    result = await container.services.get(CommitReservation).execute(reservation_id)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_STOCK_UNAVAILABLE"
    assert (await reservation_row(container, reservation_id))["status"] == "expired"
    assert await item_row(container, presentation_id) == (2, 2)
    assert await sale_movements(container, presentation_id) == []


async def test_a_loss_below_reserved_is_recorded_then_the_commit_is_refused(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 3)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")

    await lose(container, presentation_id, 2)  # allowed: the loss reflects the warehouse

    stock = value_of(await container.services.get(GetStock).execute(presentation_id))
    assert (stock.on_hand, stock.reserved, stock.available) == (1, 2, 0)
    await relay_all(container)
    assert await statuses(container, perfume_id) == {100: "sold_out"}
    movements = await table_count(container, "stock_movements")

    result = await container.services.get(CommitReservation).execute(reservation_id)

    assert isinstance(result, Err)
    assert result.error.code == "INVENTORY_STOCK_INSUFFICIENT"
    assert result.error.details == {
        "lines": [{"presentation_id": str(presentation_id), "requested": 2, "on_hand": 1}]
    }
    assert await item_row(container, presentation_id) == (1, 2)
    assert (await reservation_row(container, reservation_id))["status"] == "active"
    assert await table_count(container, "stock_movements") == movements


async def test_the_stock_list_uses_available_for_the_low_flag(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    await reserve_ok(container, {presentation_id: 8}, "QA-1")

    page = await container.services.get(ListStock).execute(low=True, page=1, size=50)

    [line] = [line for line in page.items if line.presentation_id == presentation_id]
    assert (line.on_hand, line.reserved, line.available, line.is_low) == (10, 8, 2, True)


async def test_the_facade_in_the_container_drives_a_reservation_end_to_end(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    facade = container.services.get(StockReservations)

    reserved = await facade.reserve("QA-1", {presentation_id: 2}, datetime.now(UTC) + timedelta(1))
    reservation_id = value_of(reserved)
    committed = await facade.commit(reservation_id)

    assert isinstance(committed, Ok)
    assert await item_row(container, presentation_id) == (3, 0)
