"""Plan 005 step 3, migration 0022 (`orders_lookup_throttle`), proved on a SCRATCH database (never
`fragancia_test`) with the real Alembic CLI, like the 0017 to 0021 tests whose scratch-database
fixture it reuses.

Covers: the table does not exist at 0021; the upgrade creates `orders.lookup_throttle` with the
planned columns (key varchar 64 primary key, attempts and window_started_at not null); the primary
key refuses a duplicated key and the columns refuse nulls; existing orders are untouched by the
upgrade; the downgrade removes only the table; and a second upgrade rebuilds exactly the same
schema (the round trip)."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError

from tests.integration.orders.test_migration_0017_delivery_settings import Scratch, revision
from tests.integration.orders.test_migration_0019_orders_orders import insert_order
from tests.integration.orders.test_migration_0021_orders_lifecycle import (
    check_definitions,
    columns,
    foreign_key_actions,
    index_definitions,
    tables,
)

pytestmark = pytest.mark.integration

BEFORE = "0021"
AFTER = "0022"

COLUMNS: dict[str, tuple[str, str, int | None]] = {
    "key": ("character varying", "NO", 64),
    "attempts": ("integer", "NO", None),
    "window_started_at": ("timestamp with time zone", "NO", None),
}


def primary_key(engine: Engine) -> tuple[str, list[str]]:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT c.conname, a.attname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                "WHERE c.contype = 'p' AND c.conrelid = 'orders.lookup_throttle'::regclass "
                "ORDER BY a.attnum"
            )
        ).all()
    return found[0][0], [row[1] for row in found]


def snapshot(engine: Engine) -> dict[str, object]:
    return {
        "tables": tables(engine),
        "orders": columns(engine, "orders"),
        "throttle": columns(engine, "lookup_throttle"),
        "indexes": index_definitions(engine),
        "checks": check_definitions(engine),
        "foreign_keys": foreign_key_actions(engine),
    }


def hit_row(engine: Engine, key: str = "lookup-ip:1.1.1.1") -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.lookup_throttle (key, attempts, window_started_at) "
                "VALUES (:k, 1, now())"
            ),
            {"k": key},
        )


def test_the_table_does_not_exist_at_the_previous_revision(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert "lookup_throttle" not in tables(engine)


def test_upgrade_creates_the_table_with_the_planned_columns(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    assert columns(engine, "lookup_throttle") == COLUMNS


def test_the_key_is_the_primary_key(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    assert primary_key(engine) == ("pk_lookup_throttle", ["key"])


def test_a_duplicated_key_is_refused(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    hit_row(engine)

    with pytest.raises(IntegrityError, match="pk_lookup_throttle"):
        hit_row(engine)


def test_the_columns_refuse_nulls(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    for key, attempts, started in (
        (None, 1, "now"),
        ("k", None, "now"),
        ("k", 1, None),
    ):
        with pytest.raises(IntegrityError, match="null value"), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO orders.lookup_throttle (key, attempts, window_started_at) "
                    "VALUES (:k, :a, CAST(:s AS timestamptz))"
                ),
                {"k": key, "a": attempts, "s": started},
            )


def test_a_key_over_64_characters_is_refused(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with pytest.raises(DataError):
        hit_row(engine, "k" * 65)
    hit_row(engine, "k" * 64)


def test_an_order_stored_before_the_upgrade_is_untouched(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    order_id = insert_order(engine, number=1005, total=1_100)

    scratch.alembic("upgrade", AFTER)

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT number, status, total_cents FROM orders.orders WHERE id = :id"),
            {"id": order_id},
        ).one()
    assert tuple(row) == (1005, "pending_payment", 1_100)


def test_downgrade_removes_only_the_table_and_keeps_the_orders(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    insert_order(engine)
    hit_row(engine)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert "lookup_throttle" not in tables(engine)
    assert {"orders", "order_lines", "order_events", "order_notes"} <= tables(engine)
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


def test_the_table_works_again_after_a_round_trip(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    scratch.alembic("downgrade", BEFORE)
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    hit_row(engine)

    with pytest.raises(IntegrityError, match="pk_lookup_throttle"):
        hit_row(engine)
