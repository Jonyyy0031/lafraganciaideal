"""Plan 003, migration 0019 (`orders.order_numbers`, `orders.orders`, `orders.order_lines`), proved
on a SCRATCH database (never `fragancia_test`) with the real Alembic CLI, like the 0017 and 0018
tests, whose scratch-database fixture it reuses. The implementer did not run the downgrade; this
file does.

Upgrades to 0018 and checks the order tables are absent, upgrades to 0019 and checks the
columns, the seeded counter, the constraints and the index, downgrades one step (also with orders
stored) and checks only the order tables are gone (the carts and the delivery settings stay),
and upgrades again."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from fragancia_api.shared.kernel import new_id
from tests.integration.orders.test_migration_0017_delivery_settings import Scratch, revision

pytestmark = pytest.mark.integration

BEFORE = "0018"
AFTER = "0019"

UUID = ("uuid", "NO", None)
INT = ("integer", "NO", None)
INT_NULL = ("integer", "YES", None)
TIMESTAMP = ("timestamp with time zone", "NO", None)


def varchar(length: int, nullable: bool = False) -> tuple[str, str, int | None]:
    return ("character varying", "YES" if nullable else "NO", length)


# table -> column -> (data type, nullable, character maximum length)
COLUMNS: dict[str, dict[str, tuple[str, str, int | None]]] = {
    "order_numbers": {"id": INT, "last_number": INT},
    "orders": {
        "id": UUID,
        "number": INT,
        "status": varchar(30),
        "access_token_digest": varchar(64),
        "customer_name": varchar(120),
        "customer_email": varchar(254),
        "customer_phone": varchar(10),
        "delivery_method": varchar(10),
        "delivery_cents": INT,
        "address_street": varchar(120, True),
        "address_exterior_number": varchar(20, True),
        "address_interior_number": varchar(20, True),
        "address_neighborhood": varchar(120, True),
        "address_postal_code": varchar(5, True),
        "address_municipality": varchar(120, True),
        "address_state": varchar(3, True),
        "address_references": varchar(300, True),
        "pickup_address": varchar(300, True),
        "pickup_hours": varchar(200, True),
        "invoice_rfc": varchar(13, True),
        "invoice_business_name": varchar(254, True),
        "invoice_tax_regime": varchar(3, True),
        "invoice_cfdi_use": varchar(3, True),
        "invoice_postal_code": varchar(5, True),
        "subtotal_cents": INT,
        "total_cents": INT,
        "lead_time_max_days": INT_NULL,
        "reservation_id": ("uuid", "YES", None),
        "payment_due_at": TIMESTAMP,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
    },
    "order_lines": {
        "order_id": UUID,
        "position": INT,
        "presentation_id": UUID,
        "perfume_id": UUID,
        "label": varchar(300),
        "ml": INT,
        "availability": varchar(20),
        "lead_time_min_days": INT_NULL,
        "lead_time_max_days": INT_NULL,
        "quantity": INT,
        "unit_price_cents": INT,
        "regular_price_cents": INT,
        "line_total_cents": INT,
    },
}
ORDER_TABLES = {"order_numbers", "orders", "order_lines"}
OLD_TABLES = {"delivery_settings", "carts", "cart_lines"}


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


def scalar(engine: Engine, query: str) -> object:
    with engine.connect() as connection:
        return connection.execute(text(query)).scalar_one()


def insert_order(
    engine: Engine,
    *,
    number: int = 1001,
    digest: str = "a" * 64,
    method: str = "home",
    delivery: int = 100,
    subtotal: int = 1_000,
    total: int = 1_100,
) -> object:
    order_id = new_id()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.orders (id, number, status, access_token_digest, "
                "customer_name, customer_email, customer_phone, delivery_method, delivery_cents, "
                "subtotal_cents, total_cents, payment_due_at, created_at, updated_at) VALUES "
                "(:id, :number, 'pending_payment', :digest, 'Ana', 'a@b.co', '9981234567', "
                ":method, :delivery, :subtotal, :total, now(), now(), now())"
            ),
            {
                "id": order_id,
                "number": number,
                "digest": digest,
                "method": method,
                "delivery": delivery,
                "subtotal": subtotal,
                "total": total,
            },
        )
    return order_id


def insert_line(
    engine: Engine,
    order_id: object,
    *,
    position: int = 0,
    quantity: int = 2,
    unit: int = 100,
    total: int = 200,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.order_lines (order_id, position, presentation_id, perfume_id, "
                "label, ml, availability, quantity, unit_price_cents, regular_price_cents, "
                "line_total_cents) VALUES (:o, :p, :pres, :perf, 'x', 50, 'in_stock', :q, :u, :u, "
                ":t)"
            ),
            {
                "o": order_id,
                "p": position,
                "pres": new_id(),
                "perf": new_id(),
                "q": quantity,
                "u": unit,
                "t": total,
            },
        )


def index_columns(engine: Engine, table: str, *, primary: bool = False) -> list[str]:
    with engine.connect() as connection:
        return sorted(
            connection.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = CAST(:table AS regclass) AND i.indisprimary = :primary "
                    "AND (:primary OR NOT i.indisunique)"
                ),
                {"table": f"orders.{table}", "primary": primary},
            )
            .scalars()
            .all()
        )


def test_the_order_tables_do_not_exist_before_the_migration(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert tables(engine) == OLD_TABLES


def test_upgrade_creates_the_three_tables_with_the_planned_columns(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    assert tables(engine) == OLD_TABLES | ORDER_TABLES
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


def test_upgrade_seeds_the_counter_so_the_first_order_is_1001(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with engine.connect() as connection:
        found = connection.execute(text("SELECT id, last_number FROM orders.order_numbers")).all()

    assert [tuple(row) for row in found] == [(1, 1000)]


def test_the_primary_keys_and_the_email_index(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    assert index_columns(engine, "order_numbers", primary=True) == ["id"]
    assert index_columns(engine, "orders", primary=True) == ["id"]
    assert index_columns(engine, "order_lines", primary=True) == ["order_id", "position"]
    assert index_columns(engine, "orders") == ["customer_email"]


def test_the_number_and_the_access_token_digest_are_unique(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    insert_order(engine, number=1001, digest="a" * 64)

    with pytest.raises(IntegrityError):
        insert_order(engine, number=1001, digest="b" * 64)
    with pytest.raises(IntegrityError):
        insert_order(engine, number=1002, digest="a" * 64)


@pytest.mark.parametrize(
    ("method", "accepted"), [("home", True), ("local", True), ("pickup", True), ("courier", False)]
)
def test_the_delivery_method_check(scratch: Scratch, method: str, accepted: bool) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    if accepted:
        insert_order(engine, method=method)
    else:
        with pytest.raises(IntegrityError):
            insert_order(engine, method=method)


def test_the_total_must_be_the_subtotal_plus_the_delivery(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    insert_order(engine, delivery=100, subtotal=1_000, total=1_100)
    with pytest.raises(IntegrityError):
        insert_order(engine, number=1002, digest="b" * 64, total=1_099)


@pytest.mark.parametrize(("quantity", "accepted"), [(0, False), (1, True), (10, True), (11, False)])
def test_the_line_quantity_check_accepts_1_to_10_only(
    scratch: Scratch, quantity: int, accepted: bool
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)

    if accepted:
        insert_line(engine, order_id, quantity=quantity, total=100 * quantity)
    else:
        with pytest.raises(IntegrityError):
            insert_line(engine, order_id, quantity=quantity, total=100 * quantity)


def test_the_line_total_must_be_the_unit_price_times_the_quantity(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)

    insert_line(engine, order_id, quantity=3, unit=100, total=300)
    with pytest.raises(IntegrityError):
        insert_line(engine, order_id, position=1, quantity=3, unit=100, total=301)


def test_a_position_appears_once_per_order_and_a_line_needs_its_order(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)
    insert_line(engine, order_id, position=0)

    with pytest.raises(IntegrityError):
        insert_line(engine, order_id, position=0)
    with pytest.raises(IntegrityError):
        insert_line(engine, new_id())


def test_deleting_an_order_cascades_to_its_lines(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    order_id = insert_order(engine)
    insert_line(engine, order_id, position=0)
    insert_line(engine, order_id, position=1)
    assert scalar(engine, "SELECT count(*) FROM orders.order_lines") == 2

    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM orders.orders WHERE id = :id"), {"id": order_id}
            ).scalar_one()
            == 1
        )
        connection.execute(text("DELETE FROM orders.orders WHERE id = :id"), {"id": order_id})

    assert scalar(engine, "SELECT count(*) FROM orders.order_lines") == 0


def test_downgrade_drops_only_the_order_tables(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert tables(engine) == OLD_TABLES
    assert scalar(engine, "SELECT count(*) FROM orders.delivery_settings") == 1


def test_downgrade_works_with_orders_and_lines_stored_and_keeps_the_carts(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.carts (id, token_digest, created_at, updated_at) "
                "VALUES (:id, :digest, now(), now())"
            ),
            {"id": new_id(), "digest": "c" * 64},
        )
    order_id = insert_order(engine)
    insert_line(engine, order_id)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert tables(engine) == OLD_TABLES
    assert scalar(engine, "SELECT count(*) FROM orders.carts") == 1
    assert scalar(engine, "SELECT count(*) FROM orders.delivery_settings") == 1


def test_a_round_trip_leaves_empty_order_tables_and_a_reseeded_counter(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    insert_line(engine, insert_order(engine))
    with engine.begin() as connection:
        connection.execute(text("UPDATE orders.order_numbers SET last_number = 1500 WHERE id = 1"))

    scratch.alembic("downgrade", BEFORE)
    scratch.alembic("upgrade", AFTER)

    assert revision(engine) == AFTER
    assert tables(engine) == OLD_TABLES | ORDER_TABLES
    assert scalar(engine, "SELECT count(*) FROM orders.orders") == 0
    assert scalar(engine, "SELECT count(*) FROM orders.order_lines") == 0
    assert scalar(engine, "SELECT count(*) FROM orders.order_numbers") == 1
    assert scalar(engine, "SELECT last_number FROM orders.order_numbers WHERE id = 1") == 1000
