"""Plan 002, migration 0018 (`orders.carts` and `orders.cart_lines`), proved on a SCRATCH database
(never `fragancia_test`) with the real Alembic CLI, like the 0017 test, whose scratch-database
fixture it reuses.

Upgrades to 0017 and checks the carts are absent, upgrades to 0018 and checks the columns, the
constraints and the index, downgrades one step and checks only the cart tables are gone (the
delivery settings and their default row stay), and upgrades again."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from fragancia_api.shared.kernel import new_id
from tests.integration.orders.test_migration_0017_delivery_settings import Scratch, revision

pytestmark = pytest.mark.integration

BEFORE = "0017"
AFTER = "0018"

# table -> column -> (data type, nullable, character maximum length)
COLUMNS: dict[str, dict[str, tuple[str, str, int | None]]] = {
    "carts": {
        "id": ("uuid", "NO", None),
        "token_digest": ("character varying", "NO", 64),
        "created_at": ("timestamp with time zone", "NO", None),
        "updated_at": ("timestamp with time zone", "NO", None),
    },
    "cart_lines": {
        "cart_id": ("uuid", "NO", None),
        "presentation_id": ("uuid", "NO", None),
        "quantity": ("integer", "NO", None),
        "seen_price_cents": ("integer", "NO", None),
        "added_at": ("timestamp with time zone", "NO", None),
    },
}


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


def insert_cart(engine: Engine, digest: str = "d" * 64) -> object:
    cart_id = new_id()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.carts (id, token_digest, created_at, updated_at) "
                "VALUES (:id, :digest, now(), now())"
            ),
            {"id": cart_id, "digest": digest},
        )
    return cart_id


def insert_line(
    engine: Engine, cart_id: object, quantity: int = 1, presentation: object = None
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.cart_lines (cart_id, presentation_id, quantity, "
                "seen_price_cents, added_at) VALUES (:c, :p, :q, 100, now())"
            ),
            {"c": cart_id, "p": presentation or new_id(), "q": quantity},
        )


def test_the_cart_tables_do_not_exist_before_the_migration(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert tables(engine) == {"delivery_settings"}


def test_upgrade_creates_both_tables_with_the_planned_columns(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    assert tables(engine) == {"delivery_settings", "carts", "cart_lines"}
    with engine.connect() as connection:
        for table, expected in COLUMNS.items():
            found = {
                row[0]: (row[1], row[2], row[3])
                for row in connection.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable, character_maximum_length "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'orders' AND table_name = :t"
                    ),
                    {"t": table},
                )
            }
            assert found == expected, table


def test_updated_at_is_indexed(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with engine.connect() as connection:
        indexed = set(
            connection.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = 'orders.carts'::regclass AND NOT i.indisprimary "
                    "AND NOT i.indisunique"
                )
            )
            .scalars()
            .all()
        )
    assert indexed == {"updated_at"}


def test_the_token_digest_is_unique_and_the_line_key_is_cart_and_presentation(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    cart_id = insert_cart(engine)
    presentation = new_id()
    insert_line(engine, cart_id, presentation=presentation)

    with pytest.raises(IntegrityError):
        insert_cart(engine)
    with pytest.raises(IntegrityError):
        insert_line(engine, cart_id, presentation=presentation)
    with engine.connect() as connection:
        key = sorted(
            connection.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = 'orders.cart_lines'::regclass AND i.indisprimary"
                )
            )
            .scalars()
            .all()
        )
    assert key == ["cart_id", "presentation_id"]


@pytest.mark.parametrize(("quantity", "accepted"), [(0, False), (1, True), (10, True), (11, False)])
def test_the_quantity_check_accepts_1_to_10_only(
    scratch: Scratch, quantity: int, accepted: bool
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    cart_id = insert_cart(engine)

    if accepted:
        insert_line(engine, cart_id, quantity)
    else:
        with pytest.raises(IntegrityError):
            insert_line(engine, cart_id, quantity)


def test_deleting_a_cart_cascades_to_its_lines(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    cart_id = insert_cart(engine)
    insert_line(engine, cart_id)
    insert_line(engine, cart_id)

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM orders.carts WHERE id = :id"), {"id": cart_id})

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM orders.cart_lines")).scalar_one() == 0


def test_a_line_needs_an_existing_cart(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with pytest.raises(IntegrityError):
        insert_line(engine, new_id())


def test_downgrade_drops_only_the_cart_tables(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert tables(engine) == {"delivery_settings"}
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM orders.delivery_settings")).scalar_one()
            == 1
        )


def test_a_round_trip_leaves_empty_cart_tables(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    insert_line(engine, insert_cart(engine))

    scratch.alembic("downgrade", BEFORE)
    scratch.alembic("upgrade", AFTER)

    assert revision(engine) == AFTER
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM orders.carts")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM orders.cart_lines")).scalar_one() == 0
