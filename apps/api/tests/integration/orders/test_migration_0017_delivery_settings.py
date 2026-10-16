"""Plan 001, migration 0017 (`orders.delivery_settings` and its default row), proved on a SCRATCH
database, never on `fragancia_test`: the test creates `fragancia_mig_<random>_test` next to it,
migrates it with the real Alembic CLI in a subprocess (so no other suite can race the downgrade,
and Alembic's logging setup stays out of this process), and drops it afterwards.

Upgrades to 0016 and checks nothing of orders exists, upgrades to 0017 and checks the schema,
the columns and the default row, downgrades one step and checks the table and the schema are
gone, and upgrades again. Every subprocess is bounded by `ALEMBIC_TIMEOUT`. If the database user
cannot create a database the test is skipped as NOT CONFIRMED."""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError

from fragancia_api.config import API_ROOT, Settings
from fragancia_api.shared.kernel import new_id

pytestmark = pytest.mark.integration

ALEMBIC_TIMEOUT = 120
BEFORE = "0016"
AFTER = "0017"

# column -> (data type, nullable, character maximum length)
COLUMNS: dict[str, tuple[str, str, int | None]] = {
    "id": ("integer", "NO", None),
    "home_enabled": ("boolean", "NO", None),
    "home_price_cents": ("integer", "NO", None),
    "home_free_from_cents": ("integer", "YES", None),
    "local_enabled": ("boolean", "NO", None),
    "local_price_cents": ("integer", "NO", None),
    "local_postal_codes": ("ARRAY", "NO", None),
    "pickup_enabled": ("boolean", "NO", None),
    "pickup_address": ("character varying", "NO", 300),
    "pickup_hours": ("character varying", "NO", 200),
}

# The seeded default: every method off, prices 0, no codes, empty strings.
DEFAULT_ROW: tuple[object, ...] = (1, False, 0, None, False, 0, [], False, "", "")


class Scratch:
    """One throwaway database plus the Alembic CLI pointed at it."""

    def __init__(self, admin: Engine, name: str, url: str) -> None:
        self.admin = admin
        self.name = name
        self.url = url
        self.engine: Engine | None = None

    def alembic(self, *args: str) -> None:
        environment = {**os.environ, "DATABASE_URL_TEST": self.url}
        done = subprocess.run(  # noqa: S603  (fixed argv, the test's own scratch database)
            [sys.executable, "-m", "alembic", "-x", "test=true", *args],
            cwd=Path(API_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=ALEMBIC_TIMEOUT,
            check=False,
        )
        assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stderr[-1500:]}"

    def connect(self) -> Engine:
        if self.engine is None:
            self.engine = create_engine(self.url)
        return self.engine


@pytest.fixture
def scratch(settings: Settings) -> Iterator[Scratch]:
    assert settings.database_url_test is not None
    base = make_url(settings.database_url_test)
    name = f"fragancia_mig_{new_id().hex[:12]}_test"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    world = Scratch(admin, name, base.set(database=name).render_as_string(hide_password=False))
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except ProgrammingError as error:
        admin.dispose()
        pytest.skip(f"NOT CONFIRMED: this database user cannot create a scratch database: {error}")
    try:
        yield world
    finally:
        if world.engine is not None:
            world.engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT version_num FROM platform.alembic_version")
            ).scalar_one()
        )


def schema_exists(engine: Engine) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'orders'"
                )
            ).scalar_one()
        )


def table_exists(engine: Engine) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'orders' AND table_name = 'delivery_settings'"
                )
            ).scalar_one()
        )


def rows(engine: Engine) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT id, home_enabled, home_price_cents, home_free_from_cents, "
                    "local_enabled, local_price_cents, local_postal_codes, pickup_enabled, "
                    "pickup_address, pickup_hours FROM orders.delivery_settings"
                )
            )
        ]


def test_nothing_of_orders_exists_before_the_migration(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()

    assert revision(engine) == BEFORE
    assert not schema_exists(engine)
    assert not table_exists(engine)


def test_upgrade_creates_the_schema_the_table_and_the_default_row(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)

    scratch.alembic("upgrade", AFTER)

    engine = scratch.connect()
    assert revision(engine) == AFTER
    assert schema_exists(engine)
    assert table_exists(engine)
    assert rows(engine) == [DEFAULT_ROW]


def test_the_columns_have_the_types_nullability_and_lengths_of_the_plan(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with engine.connect() as connection:
        found = {
            row[0]: (row[1], row[2], row[3])
            for row in connection.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'orders' AND table_name = 'delivery_settings'"
                )
            )
        }
    assert found == COLUMNS


def test_the_table_has_a_primary_key_on_id_and_rejects_a_second_row_with_it(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()

    with engine.connect() as connection:
        key = (
            connection.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = 'orders.delivery_settings'::regclass AND i.indisprimary"
                )
            )
            .scalars()
            .all()
        )
    assert key == ["id"]
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders.delivery_settings VALUES "
                "(1, false, 0, NULL, false, 0, ARRAY[]::varchar(5)[], false, '', '')"
            )
        )


def test_downgrade_drops_the_table_and_the_schema(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    assert table_exists(engine)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert not table_exists(engine)
    assert not schema_exists(engine)


def test_a_round_trip_restores_the_default_row_and_discards_the_edited_settings(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE orders.delivery_settings SET home_enabled = true, "
                "home_price_cents = 18000, local_postal_codes = ARRAY['77500']"
            )
        )
    assert rows(engine) != [DEFAULT_ROW]

    scratch.alembic("downgrade", BEFORE)
    scratch.alembic("upgrade", AFTER)

    assert revision(engine) == AFTER
    assert rows(engine) == [DEFAULT_ROW]


def test_upgrading_again_to_head_after_the_round_trip_keeps_one_default_row(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", AFTER)
    scratch.alembic("downgrade", BEFORE)
    scratch.alembic("upgrade", "head")

    engine = scratch.connect()
    assert table_exists(engine)
    assert rows(engine) == [DEFAULT_ROW]
