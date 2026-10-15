"""Plan 004, migration 0015 (`inventory.stock_items.low_alerted`) and its backfill, proved on a
SCRATCH database, never on `fragancia_test`: the test creates `fragancia_mig_<random>_test` next to
it, migrates it with the real Alembic CLI in a subprocess (so no other suite can race the
downgrade, and Alembic's logging setup stays out of this process), and drops it afterwards.

Seeds the stock at revision 0014, upgrades, and checks which rows the backfill marked; then
downgrades one step, checks the column is gone and the rows survive, and upgrades again.
Every subprocess is bounded by `ALEMBIC_TIMEOUT`. If the database user cannot create a database
the test is skipped as NOT CONFIRMED."""

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ProgrammingError

from fragancia_api.config import API_ROOT, Settings
from fragancia_api.shared.kernel import new_id

pytestmark = pytest.mark.integration

ALEMBIC_TIMEOUT = 120
BEFORE = "0014"
AFTER = "0015"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

# name -> (on_hand, reserved, threshold_override)
ITEMS: dict[str, tuple[int, int, int | None]] = {
    "equal_to_global": (3, 0, None),
    "above_global": (4, 0, None),
    "zero": (0, 0, None),
    "below_a_higher_override": (6, 0, 10),
    "above_a_lower_override": (2, 0, 1),
    "equal_to_override": (5, 0, 5),
    "reserved_but_on_hand_above_global": (4, 3, None),  # available 1: low for the panel, not here
}

# Expected `low_alerted` per global threshold the settings row holds (None: no settings row).
EXPECTED: dict[int | None, dict[str, bool]] = {
    3: {
        "equal_to_global": True,
        "above_global": False,
        "zero": True,
        "below_a_higher_override": True,
        "above_a_lower_override": False,
        "equal_to_override": True,
        "reserved_but_on_hand_above_global": False,
    },
    5: {
        "equal_to_global": True,
        "above_global": True,
        "zero": True,
        "below_a_higher_override": True,
        "above_a_lower_override": False,
        "equal_to_override": True,
        "reserved_but_on_hand_above_global": True,
    },
    # No settings row: the migration falls back to 3, the seeded default.
    None: {
        "equal_to_global": True,
        "above_global": False,
        "zero": True,
        "below_a_higher_override": True,
        "above_a_lower_override": False,
        "equal_to_override": True,
        "reserved_but_on_hand_above_global": False,
    },
}


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


def seed(engine: Engine, global_threshold: int | None) -> dict[str, UUID]:
    ids = {name: new_id() for name in ITEMS}
    with engine.begin() as connection:
        if global_threshold is None:
            connection.execute(text("DELETE FROM inventory.settings WHERE id = 1"))
        else:
            connection.execute(
                text("UPDATE inventory.settings SET low_stock_threshold = :v WHERE id = 1"),
                {"v": global_threshold},
            )
        for name, (on_hand, reserved, override) in ITEMS.items():
            connection.execute(
                text(
                    "INSERT INTO inventory.stock_items "
                    "(presentation_id, on_hand, reserved, threshold_override, updated_at) "
                    "VALUES (:id, :on_hand, :reserved, :override, :now)"
                ),
                {
                    "id": ids[name],
                    "on_hand": on_hand,
                    "reserved": reserved,
                    "override": override,
                    "now": NOW,
                },
            )
    return ids


def flags(engine: Engine, ids: dict[str, UUID]) -> dict[str, bool]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT presentation_id, low_alerted FROM inventory.stock_items")
        ).all()
    by_id = {row[0]: bool(row[1]) for row in rows}
    return {name: by_id[presentation_id] for name, presentation_id in ids.items()}


def has_column(engine: Engine) -> bool:
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "table_schema = 'inventory' AND table_name = 'stock_items' "
                "AND column_name = 'low_alerted'"
            )
        ).scalar_one()
    return bool(found == 1)


def revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT version_num FROM platform.alembic_version")
            ).scalar_one()
        )


@pytest.mark.parametrize(
    "global_threshold", [3, 5, None], ids=["global-3", "global-5", "no-settings-row"]
)
def test_the_backfill_marks_exactly_the_items_already_low_by_units_on_hand(
    scratch: Scratch, global_threshold: int | None
) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    assert not has_column(engine)
    ids = seed(engine, global_threshold)

    scratch.alembic("upgrade", AFTER)

    assert revision(engine) == AFTER
    assert flags(engine, ids) == EXPECTED[global_threshold]


def test_the_column_defaults_to_false_for_rows_inserted_afterwards(scratch: Scratch) -> None:
    scratch.alembic("upgrade", AFTER)
    engine = scratch.connect()
    presentation_id = new_id()

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO inventory.stock_items (presentation_id, on_hand, updated_at) "
                "VALUES (:id, 0, :now)"
            ),
            {"id": presentation_id, "now": NOW},
        )
        stored = connection.execute(
            text("SELECT low_alerted FROM inventory.stock_items WHERE presentation_id = :id"),
            {"id": presentation_id},
        ).scalar_one()
        nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns WHERE "
                "table_schema = 'inventory' AND table_name = 'stock_items' "
                "AND column_name = 'low_alerted'"
            )
        ).scalar_one()

    assert stored is False
    assert nullable == "NO"


def test_downgrade_drops_the_column_keeps_the_rows_and_upgrade_backfills_again(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    ids = seed(engine, 3)
    scratch.alembic("upgrade", AFTER)
    assert flags(engine, ids) == EXPECTED[3]

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert not has_column(engine)
    with engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT count(*) FROM inventory.stock_items")
        ).scalar_one()
    assert remaining == len(ITEMS)

    scratch.alembic("upgrade", AFTER)

    assert revision(engine) == AFTER
    assert has_column(engine)
    assert flags(engine, ids) == EXPECTED[3]


def test_a_flag_an_item_lost_while_migrated_is_recomputed_not_kept_by_a_round_trip(
    scratch: Scratch,
) -> None:
    """The round trip re-derives the flag from the stock: an item restocked above its threshold
    between the downgrade and the upgrade comes back un-alerted."""
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    ids = seed(engine, 3)
    scratch.alembic("upgrade", AFTER)
    scratch.alembic("downgrade", BEFORE)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE inventory.stock_items SET on_hand = 9 WHERE presentation_id = :id"),
            {"id": ids["zero"]},
        )

    scratch.alembic("upgrade", AFTER)

    assert flags(engine, ids)["zero"] is False
    assert flags(engine, ids)["equal_to_global"] is True
