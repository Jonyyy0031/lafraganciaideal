"""Plan 004 step 5, migration 0021 (`orders_lifecycle`), proved on a SCRATCH database (never
`fragancia_test`) with the real Alembic CLI, like the 0017 to 0020 tests whose scratch-database
fixture it reuses.

Covers: nothing of the lifecycle exists at 0020; the upgrade adds the four `orders.orders`
columns, the `order_events` and `order_notes` tables, the status and refund checks and the two
indexes (one partial); an order stored before the upgrade keeps its data; the checks accept the
seven statuses and refuse anything else; deleting an order cascades to its history and notes; the
downgrade removes all of it and keeps the orders; and a second upgrade rebuilds exactly the same
schema (the round trip)."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from fragancia_api.shared.kernel import new_id
from tests.integration.orders.test_migration_0017_delivery_settings import Scratch, revision
from tests.integration.orders.test_migration_0019_orders_orders import insert_order

pytestmark = pytest.mark.integration

BEFORE = "0020"
AFTER = "0021"

STATUSES = [
    "pending_payment",
    "paid",
    "preparing",
    "shipped",
    "ready_for_pickup",
    "delivered",
    "cancelled",
]
NEW_ORDER_COLUMNS: dict[str, tuple[str, str, int]] = {
    "cancel_reason": ("character varying", "YES", 300),
    "refund_status": ("character varying", "YES", 10),
    "shipping_carrier": ("character varying", "YES", 60),
    "tracking_number": ("character varying", "YES", 60),
}
EVENT_COLUMNS: dict[str, tuple[str, str, int | None]] = {
    "id": ("uuid", "NO", None),
    "order_id": ("uuid", "NO", None),
    "at": ("timestamp with time zone", "NO", None),
    "from_status": ("character varying", "NO", 30),
    "to_status": ("character varying", "NO", 30),
    "actor": ("character varying", "NO", 64),
    "note": ("character varying", "YES", 300),
    "carrier": ("character varying", "YES", 60),
    "tracking_number": ("character varying", "YES", 60),
}
NOTE_COLUMNS: dict[str, tuple[str, str, int | None]] = {
    "id": ("uuid", "NO", None),
    "order_id": ("uuid", "NO", None),
    "at": ("timestamp with time zone", "NO", None),
    "actor": ("character varying", "NO", 64),
    "text": ("character varying", "NO", 1000),
}
NEW_INDEXES = {
    "ix_order_events_order_id",
    "ix_order_notes_order_id",
    "ix_orders_payment_due_unpaid",
    "ix_orders_status",
}
NEW_CHECKS = {"ck_orders_status", "ck_orders_refund_status"}


def columns(engine: Engine, table: str) -> dict[str, tuple[str, str, int | None]]:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT column_name, data_type, is_nullable, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = 'orders' AND table_name = :t"
            ),
            {"t": table},
        ).all()
    return {row[0]: (row[1], row[2], row[3]) for row in found}


def tables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'orders'"
                )
            )
            .scalars()
            .all()
        )


def index_definitions(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        return {
            row[0]: row[1]
            for row in connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'orders'")
            ).all()
        }


def check_definitions(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        return {
            row[0]: row[1]
            for row in connection.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE contype = 'c' AND connamespace = 'orders'::regnamespace"
                )
            ).all()
        }


def foreign_key_actions(engine: Engine) -> dict[str, str]:
    """constraint name -> `ON DELETE` action code (`c` = cascade)."""
    with engine.connect() as connection:
        return {
            row[0]: row[1]
            for row in connection.execute(
                text(
                    "SELECT conname, confdeltype FROM pg_constraint "
                    "WHERE contype = 'f' AND connamespace = 'orders'::regnamespace"
                )
            ).all()
        }


def snapshot(engine: Engine) -> dict[str, object]:
    return {
        "tables": tables(engine),
        "orders": columns(engine, "orders"),
        "events": columns(engine, "order_events"),
        "notes": columns(engine, "order_notes"),
        "indexes": index_definitions(engine),
        "checks": check_definitions(engine),
        "foreign_keys": foreign_key_actions(engine),
    }


def set_status(engine: Engine, order_id: object, status: str, refund: str | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE orders.orders SET status = :s, refund_status = :r WHERE id = :id"),
            {"s": status, "r": refund, "id": order_id},
        )


def insert_event(engine: Engine, order_id: object) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.order_events (id, order_id, at, from_status, to_status, actor) "
                "VALUES (:id, :o, now(), 'pending_payment', 'paid', 'admin-1')"
            ),
            {"id": new_id(), "o": order_id},
        )


def insert_note(engine: Engine, order_id: object) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.order_notes (id, order_id, at, actor, text) "
                "VALUES (:id, :o, now(), 'admin-1', 'hola')"
            ),
            {"id": new_id(), "o": order_id},
        )


def count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        query = text(f"SELECT count(*) FROM orders.{table}")  # noqa: S608 (a literal of this file)
        return int(connection.execute(query).scalar_one())


def test_nothing_of_the_lifecycle_exists_at_the_previous_revision(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert {"order_events", "order_notes"}.isdisjoint(tables(engine))
    assert NEW_ORDER_COLUMNS.keys().isdisjoint(columns(engine, "orders"))
    assert NEW_INDEXES.isdisjoint(index_definitions(engine))
    assert NEW_CHECKS.isdisjoint(check_definitions(engine))


def test_upgrade_adds_the_four_order_columns_all_nullable(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    found = columns(engine, "orders")
    for name, expected in NEW_ORDER_COLUMNS.items():
        assert found[name] == expected, name


def test_upgrade_creates_the_history_and_notes_tables_with_the_planned_columns(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    assert columns(engine, "order_events") == EVENT_COLUMNS
    assert columns(engine, "order_notes") == NOTE_COLUMNS


def test_upgrade_creates_the_indexes_and_the_unpaid_one_is_partial(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)

    definitions = index_definitions(scratch.connect())

    assert NEW_INDEXES <= definitions.keys()
    assert "(order_id)" in definitions["ix_order_events_order_id"]
    assert "(order_id)" in definitions["ix_order_notes_order_id"]
    assert "(status)" in definitions["ix_orders_status"]
    unpaid = definitions["ix_orders_payment_due_unpaid"]
    assert "(payment_due_at)" in unpaid
    assert "WHERE" in unpaid
    assert "pending_payment" in unpaid


def test_upgrade_creates_the_status_and_refund_checks(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)

    checks = check_definitions(scratch.connect())

    assert NEW_CHECKS <= checks.keys()
    for status in STATUSES:
        assert f"'{status}'" in checks["ck_orders_status"]
    assert "'pending'" in checks["ck_orders_refund_status"]
    assert "'done'" in checks["ck_orders_refund_status"]


def test_an_order_stored_before_the_upgrade_keeps_its_data_with_empty_new_columns(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    order_id = insert_order(engine, number=1005, total=1_100)

    scratch.alembic("upgrade", AFTER)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT number, status, total_cents, cancel_reason, refund_status, "
                "shipping_carrier, tracking_number FROM orders.orders WHERE id = :id"
            ),
            {"id": order_id},
        ).one()
    assert tuple(row) == (1005, "pending_payment", 1_100, None, None, None, None)
    assert count(engine, "order_events") == 0
    assert count(engine, "order_notes") == 0


@pytest.mark.parametrize("status", STATUSES)
def test_the_status_check_accepts_each_of_the_seven_statuses(scratch: Scratch, status: str) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)

    set_status(engine, order_id, status)

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT status FROM orders.orders WHERE id = :id"), {"id": order_id}
        ).scalar_one()
    assert stored == status


@pytest.mark.parametrize("status", ["bogus", "PAID", "refunded", ""])
def test_the_status_check_refuses_anything_else(scratch: Scratch, status: str) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)

    with pytest.raises(IntegrityError, match="ck_orders_status"):
        set_status(engine, order_id, status)


@pytest.mark.parametrize("refund", ["pending", "done", None])
def test_the_refund_check_accepts_pending_done_and_null(
    scratch: Scratch, refund: str | None
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)

    set_status(engine, order_id, "cancelled", refund)


@pytest.mark.parametrize("refund", ["bogus", "Done", ""])
def test_the_refund_check_refuses_anything_else(scratch: Scratch, refund: str) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)

    with pytest.raises(IntegrityError, match="ck_orders_refund_status"):
        set_status(engine, order_id, "cancelled", refund)


def test_events_and_notes_need_an_existing_order(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with pytest.raises(IntegrityError):
        insert_event(engine, new_id())
    with pytest.raises(IntegrityError):
        insert_note(engine, new_id())


def test_deleting_an_order_deletes_its_history_and_notes(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    kept = insert_order(engine, number=1001)
    removed = insert_order(engine, number=1002, digest="b" * 64)
    for order_id in (kept, removed):
        insert_event(engine, order_id)
        insert_note(engine, order_id)
    assert set(foreign_key_actions(engine)) >= {
        "fk_order_events_order_id_orders",
        "fk_order_notes_order_id_orders",
    }

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM orders.orders WHERE id = :id"), {"id": removed})

    assert count(engine, "order_events") == 1
    assert count(engine, "order_notes") == 1


def test_downgrade_removes_the_lifecycle_and_keeps_the_orders(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)
    set_status(engine, order_id, "cancelled", "pending")
    insert_event(engine, order_id)
    insert_note(engine, order_id)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert {"order_events", "order_notes"}.isdisjoint(tables(engine))
    assert NEW_ORDER_COLUMNS.keys().isdisjoint(columns(engine, "orders"))
    assert NEW_INDEXES.isdisjoint(index_definitions(engine))
    assert NEW_CHECKS.isdisjoint(check_definitions(engine))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM orders.orders")).scalar_one() == 1


def test_the_round_trip_rebuilds_exactly_the_same_schema(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    before = snapshot(engine)
    scratch.alembic("upgrade", AFTER)
    upgraded = snapshot(engine)

    scratch.alembic("downgrade", BEFORE)
    assert snapshot(engine) == before

    scratch.alembic("upgrade", AFTER)
    assert revision(engine) == AFTER
    assert snapshot(engine) == upgraded
    assert upgraded != before


def test_the_upgrade_after_a_round_trip_still_enforces_the_checks(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    scratch.alembic("downgrade", BEFORE)
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)

    with pytest.raises(IntegrityError, match="ck_orders_status"):
        set_status(engine, order_id, "bogus")
