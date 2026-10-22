"""Plan 001 step 4, migration 0023 (`payments_checkouts`), proved on a SCRATCH database (never
`fragancia_test`) with the real Alembic CLI, reusing the scratch-database fixture of the 0017 test.

Covers: neither the schema nor the table exist at 0022; the upgrade creates `payments.checkouts`
with the planned columns, the primary key on `order_id` and no foreign key; the constraints refuse
nulls, a duplicated order and over-long values; orders stored before the upgrade are untouched; the
downgrade drops the table and the schema and leaves the orders; and a second upgrade rebuilds the
same schema (the round trip)."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError

from tests.integration.orders.test_migration_0017_delivery_settings import Scratch, revision
from tests.integration.orders.test_migration_0019_orders_orders import insert_order
from tests.integration.orders.test_migration_0021_orders_lifecycle import (
    check_definitions,
    foreign_key_actions,
    index_definitions,
)
from tests.integration.orders.test_migration_0021_orders_lifecycle import (
    tables as orders_tables,
)

pytestmark = pytest.mark.integration

BEFORE = "0022"
AFTER = "0023"

# column -> (data type, nullable, character maximum length)
COLUMNS: dict[str, tuple[str, str, int | None]] = {
    "order_id": ("uuid", "NO", None),
    "preference_id": ("character varying", "NO", 64),
    "redirect_url": ("character varying", "NO", 500),
    "expires_at": ("timestamp with time zone", "NO", None),
    "created_at": ("timestamp with time zone", "NO", None),
}

ORDER_ID = "00000000-0000-0000-0000-000000000900"


def schema_exists(engine: Engine) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.schemata "
                    "WHERE schema_name = 'payments'"
                )
            ).scalar_one()
        )


def payments_tables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'payments'"
                )
            )
            .scalars()
            .all()
        )


def columns(engine: Engine) -> dict[str, tuple[str, str, int | None]]:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT column_name, data_type, is_nullable, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = 'payments' AND table_name = 'checkouts'"
            )
        ).all()
    return {row[0]: (row[1], row[2], row[3]) for row in found}


def constraints(engine: Engine) -> list[tuple[str, str, list[str]]]:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT c.conname, c.contype::text, "
                "array_agg(a.attname::text ORDER BY a.attnum) "
                "FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                "WHERE c.conrelid = 'payments.checkouts'::regclass AND c.contype <> 'n' "
                "GROUP BY c.conname, c.contype ORDER BY c.conname"
            )
        ).all()
    return [(row[0], row[1], list(row[2])) for row in found]


def snapshot(engine: Engine) -> dict[str, object]:
    return {
        "schema": schema_exists(engine),
        "payments_tables": payments_tables(engine),
        "orders_tables": orders_tables(engine),
        "columns": columns(engine) if "checkouts" in payments_tables(engine) else None,
        "constraints": constraints(engine) if "checkouts" in payments_tables(engine) else None,
        "indexes": index_definitions(engine),
        "checks": check_definitions(engine),
        "foreign_keys": foreign_key_actions(engine),
    }


def put(
    engine: Engine,
    *,
    order_id: str | None = ORDER_ID,
    preference_id: str | None = "pref-1",
    redirect_url: str | None = "https://pay.test/1",
    expires_at: str | None = "2026-10-22T12:30:00+00",
    created_at: str | None = "2026-10-22T12:00:00+00",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO payments.checkouts "
                "(order_id, preference_id, redirect_url, expires_at, created_at) "
                "VALUES (CAST(:o AS uuid), :p, :u, CAST(:e AS timestamptz), "
                "CAST(:c AS timestamptz))"
            ),
            {
                "o": order_id,
                "p": preference_id,
                "u": redirect_url,
                "e": expires_at,
                "c": created_at,
            },
        )


def test_nothing_of_payments_exists_at_the_previous_revision(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert schema_exists(engine) is False
    assert payments_tables(engine) == set()


def test_upgrade_creates_the_schema_and_the_table_with_the_planned_columns(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    assert schema_exists(engine) is True
    assert payments_tables(engine) == {"checkouts"}
    assert columns(engine) == COLUMNS


def test_the_order_id_is_the_only_constraint_and_it_is_not_a_foreign_key(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    assert constraints(engine) == [("pk_checkouts", "p", ["order_id"])]


def test_a_checkout_needs_no_existing_order(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    put(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM payments.checkouts")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM orders.orders")).scalar_one() == 0


def test_a_second_checkout_for_the_same_order_is_refused(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    put(engine)

    with pytest.raises(IntegrityError, match="pk_checkouts"):
        put(engine, preference_id="pref-2")


@pytest.mark.parametrize(
    "column", ["order_id", "preference_id", "redirect_url", "expires_at", "created_at"]
)
def test_every_column_refuses_nulls(scratch: Scratch, column: str) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with pytest.raises(IntegrityError, match="null value"):
        put(engine, **{column: None})


def test_the_values_over_their_limits_are_refused(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with pytest.raises(DataError):
        put(engine, preference_id="p" * 65)
    with pytest.raises(DataError):
        put(engine, redirect_url="u" * 501)
    put(engine, preference_id="p" * 64, redirect_url="u" * 500)


def test_an_order_stored_before_the_upgrade_is_untouched(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    order_id = insert_order(engine, number=1005, total=1_100)

    scratch.alembic("upgrade", AFTER)

    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT number, status, total_cents FROM orders.orders WHERE id = :id"),
            {"id": order_id},
        ).one()
    assert tuple(found) == (1005, "pending_payment", 1_100)


def test_downgrade_drops_the_table_and_the_schema_and_keeps_the_orders(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    insert_order(engine)
    put(engine)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert schema_exists(engine) is False
    assert payments_tables(engine) == set()
    assert {"orders", "order_lines", "order_events", "order_notes"} <= orders_tables(engine)
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

    put(engine)

    with pytest.raises(IntegrityError, match="pk_checkouts"):
        put(engine)
