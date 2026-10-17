"""Plan 003 review repair, migration 0020 (`orders.orders.cart_token_digest`), proved on a SCRATCH
database (never `fragancia_test`) with the real Alembic CLI, like the 0019 test, whose
scratch-database fixture it reuses: the column is absent before, nullable varchar(64) and
indexed after, orders stored before keep a NULL digest, the downgrade drops column and index and
keeps the orders, and a second upgrade works."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.integration.orders.test_migration_0017_delivery_settings import Scratch, revision
from tests.integration.orders.test_migration_0019_orders_orders import insert_order

pytestmark = pytest.mark.integration

BEFORE = "0019"
AFTER = "0020"
INDEX = "ix_orders_cart_token_digest"


def column(engine: Engine) -> tuple[str, str, int] | None:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT data_type, is_nullable, character_maximum_length "
                "FROM information_schema.columns WHERE table_schema = 'orders' "
                "AND table_name = 'orders' AND column_name = 'cart_token_digest'"
            )
        ).first()
    return None if found is None else (found[0], found[1], found[2])


def indexed(engine: Engine) -> bool:
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT count(*) FROM pg_indexes WHERE schemaname = 'orders' AND indexname = :i"),
            {"i": INDEX},
        ).scalar_one()
    return int(found) == 1


def test_the_column_does_not_exist_before_the_migration(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert column(engine) is None
    assert not indexed(engine)


def test_upgrade_adds_a_nullable_indexed_varchar_64_and_keeps_old_orders(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    order_id = insert_order(engine)

    scratch.alembic("upgrade", AFTER)

    assert revision(engine) == AFTER
    assert column(engine) == ("character varying", "YES", 64)
    assert indexed(engine)
    with engine.connect() as connection:
        digest = connection.execute(
            text("SELECT cart_token_digest FROM orders.orders WHERE id = :id"), {"id": order_id}
        ).scalar_one()
    assert digest is None


def test_downgrade_drops_the_column_and_the_index_but_keeps_the_orders(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    insert_order(engine)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert column(engine) is None
    assert not indexed(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM orders.orders")).scalar_one() == 1


def test_upgrading_again_after_a_downgrade_works(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    scratch.alembic("downgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    assert column(engine) == ("character varying", "YES", 64)
