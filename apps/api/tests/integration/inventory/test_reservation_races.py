"""Plan 003 on PostgreSQL: the races the reservation locks must settle.

Every race is deterministic where it can be: one side is paused (uncommitted, holding its locks)
by a gate while the other starts, and the test says when the gate opens. The rest (several
concurrent reserves, a mixed storm) checks invariants and absence of deadlock.

A lock-order regression must fail these tests within seconds, never hang the suite: every gate is
opened and every task finished or cancelled when a test leaves its `Scenario` block (pass or
fail), and every wait and every awaited task is bounded by `TIMEOUT`."""

import asyncio
from collections.abc import Awaitable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from fragancia_api.container import Container
from fragancia_api.modules.inventory.contracts import MovementRequest
from fragancia_api.shared.kernel import Err, Ok, Result
from tests.integration.catalog.presentation_rows import CatalogRows
from tests.integration.inventory.reservation_support import (
    Commands,
    in_minutes,
    item_row,
    make_due,
    reservation_row,
    reserve_ok,
    sale_movements,
    table_count,
)
from tests.integration.inventory.test_stock_settings_race import (
    TIMEOUT,
    Scenario,
    is_blocked,
    plain,
    record_command,
    settings_command,
)
from tests.integration.inventory.test_stock_status_flow import (
    copy_of,
    outbox_rows,
    relay_all,
    restock,
)

pytestmark = pytest.mark.integration


def code_of(result: Result[Any, Any]) -> str:
    assert isinstance(result, Err)
    code: str = result.error.code
    return code


def loss(quantity: int) -> MovementRequest:
    return MovementRequest(kind="loss", quantity=quantity, reason="Rotura")


async def returns_without_waiting[T](awaitable: Awaitable[T]) -> T:
    """The call must complete by itself while the other transaction is paused: a lock wait would
    time out here and fail the test."""
    return await asyncio.wait_for(awaitable, timeout=TIMEOUT)


# --- two reserves for the last unit ------------------------------------------------------------


async def test_the_second_reserve_waits_for_the_first_and_then_finds_no_unit(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 1)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        first = scenario.spawn(
            Commands(container, gated).reserve.execute("R-1", {presentation_id: 1}, in_minutes(5))
        )
        await scenario.reached(gated.reached)
        second = scenario.spawn(
            Commands(container, plain(container)).reserve.execute(
                "R-2", {presentation_id: 1}, in_minutes(5)
            )
        )

        assert await is_blocked(second)  # it needs the item row the first one holds
        gated.release.set()
        first_result, second_result = await scenario.finish(first, second)

    assert isinstance(first_result, Ok)
    assert code_of(second_result) == "INVENTORY_STOCK_UNAVAILABLE"
    assert second_result.error.details == {
        "lines": [{"presentation_id": str(presentation_id), "requested": 1, "available": 0}]
    }
    assert await item_row(container, presentation_id) == (1, 1)
    assert await table_count(container, "reservations") == 1


async def test_many_concurrent_reserves_never_hold_more_than_is_on_hand(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 3)
    commands = Commands(container, plain(container))

    async with Scenario(container) as scenario:
        tasks = [
            scenario.spawn(commands.reserve.execute(f"R-{n}", {presentation_id: 1}, in_minutes(5)))
            for n in range(8)
        ]
        results = await scenario.finish(*tasks)

    winners = [r for r in results if isinstance(r, Ok)]
    losers = [r for r in results if isinstance(r, Err)]
    assert len(winners) == 3
    assert {r.error.code for r in losers} == {"INVENTORY_STOCK_UNAVAILABLE"}
    assert await item_row(container, presentation_id) == (3, 3)
    assert await table_count(container, "reservations") == 3
    assert await table_count(container, "reservation_lines") == 3


async def test_two_reserves_with_one_reference_create_one_reservation(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    commands = Commands(container, plain(container))

    async with Scenario(container) as scenario:
        first = scenario.spawn(
            commands.reserve.execute("SAME", {presentation_id: 2}, in_minutes(5))
        )
        second = scenario.spawn(
            commands.reserve.execute("SAME", {presentation_id: 2}, in_minutes(5))
        )
        results = await scenario.finish(first, second)

    assert sorted("ok" if isinstance(r, Ok) else code_of(r) for r in results) == [
        "INVENTORY_RESERVATION_REFERENCE_TAKEN",
        "ok",
    ]
    assert await item_row(container, presentation_id) == (10, 2)  # held once
    assert await table_count(container, "reservations") == 1


async def test_reserves_over_the_same_presentations_in_opposite_order_do_not_deadlock(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [first_id, second_id] = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 100}))
    await restock(container, first_id, 50)
    await restock(container, second_id, 50)
    commands = Commands(container, plain(container))

    async with Scenario(container) as scenario:
        tasks = []
        for n in range(10):
            quantities = {first_id: 1, second_id: 1} if n % 2 else {second_id: 1, first_id: 1}
            tasks.append(
                scenario.spawn(commands.reserve.execute(f"R-{n}", quantities, in_minutes(5)))
            )
        results = await scenario.finish(*tasks)

    assert all(isinstance(r, Ok) for r in results)
    assert await item_row(container, first_id) == (50, 10)
    assert await item_row(container, second_id) == (50, 10)


# --- a reserve and a loss ----------------------------------------------------------------------


async def test_a_loss_waits_for_a_reserve_and_may_then_leave_on_hand_below_reserved(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 3)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        reserve = scenario.spawn(
            Commands(container, gated).reserve.execute("R-1", {presentation_id: 2}, in_minutes(5))
        )
        await scenario.reached(gated.reached)
        lost = scenario.spawn(
            record_command(container, plain(container)).execute(presentation_id, loss(2), "a")
        )

        assert await is_blocked(lost)  # it needs the item row the reserve holds
        gated.release.set()
        reserve_result, loss_result = await scenario.finish(reserve, lost)

    assert isinstance(reserve_result, Ok) and isinstance(loss_result, Ok)
    assert await item_row(container, presentation_id) == (1, 2)  # allowed (decision 28)


async def test_a_reserve_waits_for_a_loss_and_sees_the_units_it_left(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 3)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        lost = scenario.spawn(
            record_command(container, gated).execute(presentation_id, loss(2), "a")
        )
        await scenario.reached(gated.reached)
        reserve = scenario.spawn(
            Commands(container, plain(container)).reserve.execute(
                "R-1", {presentation_id: 2}, in_minutes(5)
            )
        )

        assert await is_blocked(reserve)
        gated.release.set()
        loss_result, reserve_result = await scenario.finish(lost, reserve)

    assert isinstance(loss_result, Ok)
    assert code_of(reserve_result) == "INVENTORY_STOCK_UNAVAILABLE"
    assert reserve_result.error.details == {
        "lines": [{"presentation_id": str(presentation_id), "requested": 2, "available": 1}]
    }
    assert await item_row(container, presentation_id) == (1, 0)
    assert await table_count(container, "reservations") == 0


# --- a commit and the expiry job ---------------------------------------------------------------


async def due_reservation(container: Container, presentation_id: UUID, on_hand: int = 5) -> UUID:
    await restock(container, presentation_id, on_hand)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await make_due(container, reservation_id)
    return reservation_id


async def test_the_job_skips_a_reservation_a_commit_is_working_on(
    container: Container, catalog_rows: CatalogRows
) -> None:
    perfume_id, [presentation_id] = await catalog_rows.perfume()
    reservation_id = await due_reservation(container, presentation_id)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        commit = scenario.spawn(Commands(container, gated).commit.execute(reservation_id))
        await scenario.reached(gated.reached)

        expired = await returns_without_waiting(
            Commands(container, plain(container)).expire.run_once()
        )

        assert expired == 0  # SKIP LOCKED: the reservation is the commit's
        gated.release.set()
        [commit_result] = await scenario.finish(commit)

    assert isinstance(commit_result, Ok)
    assert (await reservation_row(container, reservation_id))["status"] == "committed"
    assert await item_row(container, presentation_id) == (3, 0)  # sold once, hold consumed once
    assert len(await sale_movements(container, presentation_id)) == 1
    await relay_all(container)
    assert (await copy_of(container, presentation_id))[0] == 3


async def test_a_commit_waits_for_the_job_then_sells_as_a_late_commit(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    reservation_id = await due_reservation(container, presentation_id)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        job = scenario.spawn(Commands(container, gated).expire.run_once())
        await scenario.reached(gated.reached)
        commit = scenario.spawn(
            Commands(container, plain(container)).commit.execute(reservation_id)
        )

        assert await is_blocked(commit)  # it needs the reservation row the job holds
        gated.release.set()
        expired, commit_result = await scenario.finish(job, commit)

    assert expired == 1
    assert isinstance(commit_result, Ok)  # units were available after the expiry freed them
    assert (await reservation_row(container, reservation_id))["status"] == "committed"
    assert await item_row(container, presentation_id) == (3, 0)
    assert len(await sale_movements(container, presentation_id)) == 1
    await relay_all(container)
    assert (await copy_of(container, presentation_id))[0] == 3  # the newest event wins


# --- a release and the expiry job --------------------------------------------------------------


async def test_the_job_skips_a_reservation_a_release_is_working_on(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    reservation_id = await due_reservation(container, presentation_id)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        release = scenario.spawn(Commands(container, gated).release.execute(reservation_id))
        await scenario.reached(gated.reached)

        expired = await returns_without_waiting(
            Commands(container, plain(container)).expire.run_once()
        )

        assert expired == 0
        gated.release.set()
        [release_result] = await scenario.finish(release)

    assert isinstance(release_result, Ok)
    assert (await reservation_row(container, reservation_id))["status"] == "released"
    assert await item_row(container, presentation_id) == (5, 0)  # unheld once, never below 0


async def test_a_release_waiting_for_the_job_finds_it_expired_and_changes_nothing(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    reservation_id = await due_reservation(container, presentation_id)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        job = scenario.spawn(Commands(container, gated).expire.run_once())
        await scenario.reached(gated.reached)
        release = scenario.spawn(
            Commands(container, plain(container)).release.execute(reservation_id)
        )

        assert await is_blocked(release)
        gated.release.set()
        expired, release_result = await scenario.finish(job, release)

    assert expired == 1
    assert isinstance(release_result, Ok)
    assert (await reservation_row(container, reservation_id))["status"] == "expired"
    assert await item_row(container, presentation_id) == (5, 0)


async def test_two_job_runs_at_once_expire_each_reservation_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    for n in range(3):
        await make_due(container, await reserve_ok(container, {presentation_id: 1}, f"R-{n}"))

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        first = scenario.spawn(Commands(container, gated).expire.run_once())
        await scenario.reached(gated.reached)

        second = await returns_without_waiting(
            Commands(container, plain(container)).expire.run_once()
        )

        assert second == 0  # the first run holds all three
        gated.release.set()
        [first_count] = await scenario.finish(first)

    assert first_count == 3
    assert await item_row(container, presentation_id) == (10, 0)  # unheld three times, not six


# --- two commits, two releases -----------------------------------------------------------------


async def test_two_commits_of_one_reservation_sell_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        first = scenario.spawn(Commands(container, gated).commit.execute(reservation_id))
        await scenario.reached(gated.reached)
        second = scenario.spawn(
            Commands(container, plain(container)).commit.execute(reservation_id)
        )

        assert await is_blocked(second)  # the webhook retry waits for the reservation row
        gated.release.set()
        results = await scenario.finish(first, second)

    assert all(isinstance(r, Ok) for r in results)
    assert await item_row(container, presentation_id) == (3, 0)
    assert len(await sale_movements(container, presentation_id)) == 1


async def test_two_releases_of_one_reservation_give_the_units_back_once(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    other = await reserve_ok(container, {presentation_id: 1}, "QA-2")

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        first = scenario.spawn(Commands(container, gated).release.execute(reservation_id))
        await scenario.reached(gated.reached)
        second = scenario.spawn(
            Commands(container, plain(container)).release.execute(reservation_id)
        )

        assert await is_blocked(second)
        gated.release.set()
        results = await scenario.finish(first, second)

    assert all(isinstance(r, Ok) for r in results)
    assert await item_row(container, presentation_id) == (5, 1)  # QA-2 still holds its unit
    assert (await reservation_row(container, other))["status"] == "active"


async def test_a_commit_and_a_release_of_one_reservation_end_in_one_state(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        commit = scenario.spawn(Commands(container, gated).commit.execute(reservation_id))
        await scenario.reached(gated.reached)
        release = scenario.spawn(
            Commands(container, plain(container)).release.execute(reservation_id)
        )

        assert await is_blocked(release)
        gated.release.set()
        commit_result, release_result = await scenario.finish(commit, release)

    assert isinstance(commit_result, Ok)
    assert code_of(release_result) == "INVENTORY_RESERVATION_COMMITTED"
    assert await item_row(container, presentation_id) == (3, 0)


# --- the settings row --------------------------------------------------------------------------


async def test_a_reserve_waits_for_a_settings_change_and_carries_the_new_threshold(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    await relay_all(container)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        change = scenario.spawn(settings_command(container, gated).execute(12))
        await scenario.reached(gated.reached)
        reserve = scenario.spawn(
            Commands(container, plain(container)).reserve.execute(
                "R-1", {presentation_id: 4}, in_minutes(5)
            )
        )

        assert await is_blocked(reserve)  # settings row held FOR UPDATE by the change
        gated.release.set()
        change_result, reserve_result = await scenario.finish(change, reserve)

    assert isinstance(change_result, Ok) and isinstance(reserve_result, Ok)
    await relay_all(container)
    assert (await copy_of(container, presentation_id))[:2] == (6, 12)


async def test_a_settings_change_waits_for_a_reserve_and_the_copy_ends_up_behind_neither(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 10)
    await relay_all(container)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        reserve = scenario.spawn(
            Commands(container, gated).reserve.execute("R-1", {presentation_id: 4}, in_minutes(5))
        )
        await scenario.reached(gated.reached)
        change = scenario.spawn(settings_command(container, plain(container)).execute(12))

        assert await is_blocked(change)  # needs the settings row the reserve holds in share mode
        gated.release.set()
        reserve_result, change_result = await scenario.finish(reserve, change)

    assert isinstance(reserve_result, Ok) and isinstance(change_result, Ok)
    await relay_all(container)
    # The settings event was stamped after the reserve committed and carries its available units.
    assert (await copy_of(container, presentation_id))[:2] == (6, 12)


@pytest.mark.parametrize("operation", ["commit", "release", "expire"])
async def test_a_reservation_command_waits_for_a_settings_change_without_deadlock(
    container: Container, catalog_rows: CatalogRows, operation: str
) -> None:
    """They take the reservation row, then the settings row, then the items; a settings change
    takes the settings row, then the items: one order, so no cycle."""
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await make_due(container, reservation_id)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        change = scenario.spawn(settings_command(container, gated).execute(12))
        await scenario.reached(gated.reached)
        commands = Commands(container, plain(container))
        call = (
            commands.expire.run_once()
            if operation == "expire"
            else getattr(commands, operation).execute(reservation_id)
        )
        waiting = scenario.spawn(call)

        assert await is_blocked(waiting)
        gated.release.set()
        change_result, outcome = await scenario.finish(change, waiting)

    assert isinstance(change_result, Ok)
    assert outcome == 1 if operation == "expire" else isinstance(outcome, Ok)
    expected = {"commit": (3, 0), "release": (5, 0), "expire": (5, 0)}[operation]
    assert await item_row(container, presentation_id) == expected


async def test_a_settings_change_waits_for_a_commit_in_progress_without_deadlock(
    container: Container, catalog_rows: CatalogRows
) -> None:
    _, [presentation_id] = await catalog_rows.perfume()
    await restock(container, presentation_id, 5)
    reservation_id = await reserve_ok(container, {presentation_id: 2}, "QA-1")
    await relay_all(container)

    async with Scenario(container) as scenario:
        gated = scenario.publisher()
        commit = scenario.spawn(Commands(container, gated).commit.execute(reservation_id))
        await scenario.reached(gated.reached)
        change = scenario.spawn(settings_command(container, plain(container)).execute(12))

        assert await is_blocked(change)
        gated.release.set()
        commit_result, change_result = await scenario.finish(commit, change)

    assert isinstance(commit_result, Ok) and isinstance(change_result, Ok)
    await relay_all(container)
    assert (await copy_of(container, presentation_id))[:2] == (3, 12)


# --- everything at once ------------------------------------------------------------------------


async def invariants(container: Container) -> list[tuple[int, int, int]]:
    """Per stock item: `(on_hand, reserved, units held by active reservations)`."""
    async with container.database.engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT s.on_hand, s.reserved, COALESCE(("
                "SELECT sum(l.quantity) FROM inventory.reservation_lines l "
                "JOIN inventory.reservations r ON r.id = l.reservation_id "
                "WHERE l.presentation_id = s.presentation_id AND r.status = 'active'), 0) AS held "
                "FROM inventory.stock_items s ORDER BY s.presentation_id"
            )
        )
        return [(row.on_hand, row.reserved, int(row.held)) for row in rows]


@pytest.mark.parametrize("round_number", range(3))
async def test_a_mixed_storm_keeps_reserved_equal_to_what_active_reservations_hold(
    container: Container, catalog_rows: CatalogRows, round_number: int
) -> None:
    """Reserves (both key orders), commits, releases, losses, a settings change and job runs, all
    at once. A lock-order mistake shows as a Postgres deadlock error, which fails the test."""
    _, [a, b] = await catalog_rows.perfume(sizes=({"ml": 50}, {"ml": 100}))
    await restock(container, a, 20)
    await restock(container, b, 20)
    seeded: list[UUID] = []
    for n in range(6):
        seeded.append(await reserve_ok(container, {a: 1, b: 1}, f"SEED-{round_number}-{n}"))
    for reservation_id in seeded[:2]:
        await make_due(container, reservation_id)
    commands = Commands(container, plain(container))

    async with Scenario(container) as scenario:
        tasks = [
            scenario.spawn(
                commands.reserve.execute(f"NEW-A-{round_number}", {a: 2, b: 1}, in_minutes(5))
            ),
            scenario.spawn(
                commands.reserve.execute(f"NEW-B-{round_number}", {b: 2, a: 1}, in_minutes(5))
            ),
            scenario.spawn(
                commands.reserve.execute(f"NEW-C-{round_number}", {b: 1}, in_minutes(5))
            ),
            scenario.spawn(commands.commit.execute(seeded[0])),
            scenario.spawn(commands.commit.execute(seeded[2])),
            scenario.spawn(commands.release.execute(seeded[1])),
            scenario.spawn(commands.release.execute(seeded[3])),
            scenario.spawn(commands.expire.run_once()),
            scenario.spawn(commands.expire.run_once()),
            scenario.spawn(record_command(container, plain(container)).execute(a, loss(3), "a")),
            scenario.spawn(settings_command(container, plain(container)).execute(7 + round_number)),
        ]
        results = await scenario.finish(*tasks)  # a deadlock raises here: the test fails

    for result in results:
        assert isinstance(result, (Ok, Err, int))
    for on_hand, reserved, held in await invariants(container):
        assert reserved == held
        assert on_hand >= 0
    assert await table_count(container, "stock_items") == 2
    assert len(await outbox_rows(container)) > 0
