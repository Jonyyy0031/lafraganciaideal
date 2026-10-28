"""Payments plan 003 step 4, migration 0025 (`payments_tickets`), proved on a SCRATCH database
(never `fragancia_test`) with the real Alembic CLI, reusing the scratch fixture of the 0017 test.

Covers: the four ticket columns do not exist at 0024; the upgrade adds exactly them, all nullable
and of the planned types and lengths, and nothing else changes; payments stored before the upgrade
keep their data with the new columns empty; the new columns store and refuse over-long values; the
downgrade drops only the four columns; and a second upgrade rebuilds the same schema."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError

from tests.integration.orders.test_migration_0017_delivery_settings import Scratch, revision
from tests.integration.payments.test_migration_0023_payments_checkouts import payments_tables
from tests.integration.payments.test_migration_0024_payments_payments import (
    COLUMNS as COLUMNS_0024,
)
from tests.integration.payments.test_migration_0024_payments_payments import (
    columns,
    constraints,
    payment_indexes,
    put,
    snapshot,
)

pytestmark = pytest.mark.integration

BEFORE = "0024"
AFTER = "0025"

NEW_COLUMNS: dict[str, tuple[str, str, int | None]] = {
    "payment_method": ("character varying", "YES", 32),
    "expires_at": ("timestamp with time zone", "YES", None),
    "ticket_url": ("character varying", "YES", 500),
    "held_at": ("timestamp with time zone", "YES", None),
}


def stored(engine: Engine, payment_id: str = "1234567890") -> dict[str, object]:
    with engine.connect() as connection:
        found = (
            connection.execute(
                text("SELECT * FROM payments.payments WHERE payment_id = :p"), {"p": payment_id}
            )
            .mappings()
            .one()
        )
    return dict(found)


def test_the_ticket_columns_do_not_exist_at_the_previous_revision(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert columns(engine) == COLUMNS_0024


def test_upgrade_adds_exactly_the_four_nullable_ticket_columns(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    assert columns(engine) == {**COLUMNS_0024, **NEW_COLUMNS}


def test_the_constraints_and_indexes_of_the_table_are_unchanged(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    assert constraints(engine) == [("pk_payments", "p", ["payment_id"])]
    assert set(payment_indexes(engine)) == {"pk_payments", "ix_payments_order_id"}


def test_a_payment_stored_before_the_upgrade_keeps_its_data_with_empty_ticket_columns(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    put(engine, outcome="applied")

    scratch.alembic("upgrade", AFTER)

    found = stored(engine)
    assert (found["status"], found["amount_cents"], found["outcome"]) == (
        "approved",
        268_000,
        "applied",
    )
    assert found["payment_type"] == "credit_card"
    assert [found[c] for c in NEW_COLUMNS] == [None, None, None, None]


def test_the_ticket_columns_store_a_ticket_and_refuse_over_long_values(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    put(engine)

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE payments.payments SET payment_method = :m, ticket_url = :u, "
                "expires_at = CAST(:e AS timestamptz), held_at = CAST(:h AS timestamptz) "
                "WHERE payment_id = '1234567890'"
            ),
            {
                "m": "m" * 32,
                "u": "u" * 500,
                "e": "2026-10-26T12:00:00+00",
                "h": "2026-10-24T12:01:00+00",
            },
        )
    found = stored(engine)
    assert (len(str(found["payment_method"])), len(str(found["ticket_url"]))) == (32, 500)
    for column, value in (("payment_method", "m" * 33), ("ticket_url", "u" * 501)):
        with pytest.raises(DataError), engine.begin() as connection:
            connection.execute(
                text(f"UPDATE payments.payments SET {column} = :v"),  # noqa: S608
                {"v": value},
            )


def test_downgrade_drops_only_the_ticket_columns_and_keeps_the_payments(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    put(engine)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert columns(engine) == COLUMNS_0024
    assert payments_tables(engine) == {"checkouts", "payments"}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM payments.payments")).scalar_one() == 1


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
