"""Payments plan 002 step 5, migration 0024 (`payments_payments`), proved on a SCRATCH database
(never `fragancia_test`) with the real Alembic CLI, reusing the scratch fixture of the 0017 test.

Covers: the table does not exist at 0023 (the schema and `checkouts` do); the upgrade creates
`payments.payments` with the planned columns, the primary key on `payment_id`, the non-unique
index on `order_id` and no foreign key; nulls and over-long values are refused; checkouts stored
before the upgrade are untouched; the downgrade drops only the new table and leaves the schema,
`checkouts` and the orders; and a second upgrade rebuilds the same schema (the round trip)."""

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
from tests.integration.payments.test_migration_0023_payments_checkouts import (
    ORDER_ID,
    payments_tables,
    schema_exists,
)
from tests.integration.payments.test_migration_0023_payments_checkouts import (
    put as put_checkout,
)

pytestmark = pytest.mark.integration

BEFORE = "0023"
AFTER = "0024"

# column -> (data type, nullable, character maximum length)
COLUMNS: dict[str, tuple[str, str, int | None]] = {
    "payment_id": ("character varying", "NO", 32),
    "order_id": ("uuid", "YES", None),
    "status": ("character varying", "NO", 32),
    "status_detail": ("character varying", "YES", 100),
    "amount_cents": ("bigint", "NO", None),
    "currency": ("character varying", "NO", 3),
    "payment_type": ("character varying", "YES", 32),
    "approved_at": ("timestamp with time zone", "YES", None),
    "outcome": ("character varying", "YES", 20),
    "created_at": ("timestamp with time zone", "NO", None),
    "updated_at": ("timestamp with time zone", "NO", None),
}


def columns(engine: Engine) -> dict[str, tuple[str, str, int | None]]:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT column_name, data_type, is_nullable, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = 'payments' AND table_name = 'payments'"
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
                "WHERE c.conrelid = 'payments.payments'::regclass AND c.contype <> 'n' "
                "GROUP BY c.conname, c.contype ORDER BY c.conname"
            )
        ).all()
    return [(row[0], row[1], list(row[2])) for row in found]


def payment_indexes(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'payments' AND tablename = 'payments' ORDER BY indexname"
            )
        ).all()
    return {row[0]: row[1] for row in found}


def snapshot(engine: Engine) -> dict[str, object]:
    has = "payments" in payments_tables(engine)
    return {
        "schema": schema_exists(engine),
        "payments_tables": payments_tables(engine),
        "orders_tables": orders_tables(engine),
        "columns": columns(engine) if has else None,
        "constraints": constraints(engine) if has else None,
        "payment_indexes": payment_indexes(engine) if has else None,
        "indexes": index_definitions(engine),
        "checks": check_definitions(engine),
        "foreign_keys": foreign_key_actions(engine),
    }


def put(
    engine: Engine,
    *,
    payment_id: str | None = "1234567890",
    order_id: str | None = ORDER_ID,
    status: str | None = "approved",
    status_detail: str | None = "accredited",
    amount_cents: int | None = 268_000,
    currency: str | None = "MXN",
    payment_type: str | None = "credit_card",
    approved_at: str | None = "2026-10-24T12:00:00+00",
    outcome: str | None = None,
    created_at: str | None = "2026-10-24T12:00:00+00",
    updated_at: str | None = "2026-10-24T12:00:00+00",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO payments.payments (payment_id, order_id, status, status_detail, "
                "amount_cents, currency, payment_type, approved_at, outcome, created_at, "
                "updated_at) VALUES (:p, CAST(:o AS uuid), :s, :sd, :a, :c, :t, "
                "CAST(:ap AS timestamptz), :out, CAST(:cr AS timestamptz), "
                "CAST(:up AS timestamptz))"
            ),
            {
                "p": payment_id,
                "o": order_id,
                "s": status,
                "sd": status_detail,
                "a": amount_cents,
                "c": currency,
                "t": payment_type,
                "ap": approved_at,
                "out": outcome,
                "cr": created_at,
                "up": updated_at,
            },
        )


def test_the_table_does_not_exist_at_the_previous_revision(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert payments_tables(engine) == {"checkouts"}


def test_upgrade_creates_the_table_with_the_planned_columns(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    assert payments_tables(engine) == {"checkouts", "payments"}
    assert columns(engine) == COLUMNS


def test_the_payment_id_is_the_only_constraint_and_it_is_not_a_foreign_key(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    assert constraints(engine) == [("pk_payments", "p", ["payment_id"])]


def test_the_order_id_has_a_plain_non_unique_index(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    found = payment_indexes(engine)

    assert set(found) == {"pk_payments", "ix_payments_order_id"}
    definition = found["ix_payments_order_id"]
    assert "(order_id)" in definition
    assert "UNIQUE" not in definition


def test_a_payment_needs_no_existing_order_and_two_payments_may_share_one(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    put(engine, payment_id="1")
    put(engine, payment_id="2")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM payments.payments")).scalar_one() == 2
        assert connection.execute(text("SELECT count(*) FROM orders.orders")).scalar_one() == 0


def test_a_payment_without_an_order_or_an_outcome_is_allowed(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    put(
        engine,
        order_id=None,
        status_detail=None,
        payment_type=None,
        approved_at=None,
        outcome=None,
    )

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM payments.payments")).scalar_one() == 1


def test_a_second_payment_with_the_same_id_is_refused(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    put(engine)

    with pytest.raises(IntegrityError, match="pk_payments"):
        put(engine, status="pending")


@pytest.mark.parametrize(
    "column",
    ["payment_id", "status", "amount_cents", "currency", "created_at", "updated_at"],
)
def test_the_required_columns_refuse_nulls(scratch: Scratch, column: str) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with pytest.raises(IntegrityError, match="null value"):
        put(engine, **{column: None})


def test_the_values_over_their_limits_are_refused(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with pytest.raises(DataError):
        put(engine, payment_id="9" * 33)
    with pytest.raises(DataError):
        put(engine, status="s" * 33)
    with pytest.raises(DataError):
        put(engine, status_detail="d" * 101)
    with pytest.raises(DataError):
        put(engine, currency="MXNN")
    with pytest.raises(DataError):
        put(engine, payment_type="t" * 33)
    with pytest.raises(DataError):
        put(engine, outcome="o" * 21)
    put(
        engine,
        payment_id="9" * 32,
        status="s" * 32,
        status_detail="d" * 100,
        payment_type="t" * 32,
        outcome="o" * 20,
    )


def test_amounts_beyond_32_bits_are_stored(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    put(engine, amount_cents=5_000_000_000)

    with engine.connect() as connection:
        found = connection.execute(text("SELECT amount_cents FROM payments.payments")).scalar_one()
    assert found == 5_000_000_000


def test_a_checkout_and_an_order_stored_before_the_upgrade_are_untouched(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    order_id = insert_order(engine, number=1005, total=1_100)
    put_checkout(engine)

    scratch.alembic("upgrade", AFTER)

    with engine.connect() as connection:
        order = connection.execute(
            text("SELECT number, status, total_cents FROM orders.orders WHERE id = :id"),
            {"id": order_id},
        ).one()
        checkouts = connection.execute(text("SELECT count(*) FROM payments.checkouts")).scalar_one()
    assert tuple(order) == (1005, "pending_payment", 1_100)
    assert checkouts == 1


def test_downgrade_drops_only_the_payments_table(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    insert_order(engine)
    put_checkout(engine)
    put(engine)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert schema_exists(engine) is True
    assert payments_tables(engine) == {"checkouts"}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM payments.checkouts")).scalar_one() == 1
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

    with pytest.raises(IntegrityError, match="pk_payments"):
        put(engine)
