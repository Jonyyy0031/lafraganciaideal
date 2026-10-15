"""Plan platform-api-foundation/004, migration 0016 (`platform.outbox.failed_at` and the narrower
`ix_outbox_pending`), proved on a SCRATCH database, never on `fragancia_test`: the test creates
`fragancia_mig_<random>_test` next to it, migrates it with the real Alembic CLI in a subprocess
(so no other suite can race the downgrade) and drops it afterwards. Every subprocess is bounded
by `ALEMBIC_TIMEOUT`. If the database user cannot create a database the test is skipped as
NOT CONFIRMED."""

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
BEFORE = "0015"
AFTER = "0016"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

# `pg_get_expr` of the index predicate, as PostgreSQL normalises it.
OLD_PREDICATE = "(published_at IS NULL)"
NEW_PREDICATE = "((published_at IS NULL) AND (failed_at IS NULL))"


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


def seed_pending_row(engine: Engine) -> UUID:
    """A row written while the schema is at 0015 (no `failed_at` column yet)."""
    event_id = new_id()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO platform.outbox (id, name, payload, occurred_at, attempts) "
                "VALUES (:id, 'tests.something.happened', '{}'::jsonb, :now, 7)"
            ),
            {"id": event_id, "now": NOW},
        )
    return event_id


def failed_at_column(engine: Engine) -> tuple[str, str] | None:
    """(data_type, is_nullable) of `platform.outbox.failed_at`, None when it does not exist."""
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns WHERE "
                "table_schema = 'platform' AND table_name = 'outbox' AND column_name = 'failed_at'"
            )
        ).one_or_none()
    return None if found is None else (found[0], found[1])


def pending_index(engine: Engine) -> tuple[str, str]:
    """(indexed columns, predicate) of `platform.ix_outbox_pending`."""
    with engine.connect() as connection:
        found = connection.execute(
            text(
                "SELECT pg_get_indexdef(i.indexrelid), pg_get_expr(i.indpred, i.indrelid) "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'platform' AND c.relname = 'ix_outbox_pending'"
            )
        ).one()
    return str(found[0]), str(found[1])


def revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT version_num FROM platform.alembic_version")
            ).scalar_one()
        )


def test_upgrade_adds_a_nullable_timestamptz_column_and_narrows_the_index(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    assert failed_at_column(engine) is None
    assert pending_index(engine)[1] == OLD_PREDICATE

    scratch.alembic("upgrade", AFTER)

    assert revision(engine) == AFTER
    assert failed_at_column(engine) == ("timestamp with time zone", "YES")
    definition, predicate = pending_index(engine)
    assert predicate == NEW_PREDICATE
    assert "(available_at)" in definition


def test_existing_rows_keep_failed_at_null_and_their_data(scratch: Scratch) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    event_id = seed_pending_row(engine)

    scratch.alembic("upgrade", AFTER)

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT failed_at, attempts, published_at FROM platform.outbox WHERE id = :id"),
            {"id": event_id},
        ).one()
    assert tuple(row) == (None, 7, None)


def test_downgrade_restores_the_old_index_drops_the_column_and_keeps_the_rows(
    scratch: Scratch,
) -> None:
    scratch.alembic("upgrade", BEFORE)
    engine = scratch.connect()
    event_id = seed_pending_row(engine)
    scratch.alembic("upgrade", AFTER)

    scratch.alembic("downgrade", BEFORE)

    assert revision(engine) == BEFORE
    assert failed_at_column(engine) is None
    definition, predicate = pending_index(engine)
    assert predicate == OLD_PREDICATE
    assert "(available_at)" in definition
    with engine.connect() as connection:
        kept = connection.execute(
            text("SELECT count(*) FROM platform.outbox WHERE id = :id"), {"id": event_id}
        ).scalar_one()
    assert kept == 1

    scratch.alembic("upgrade", AFTER)

    assert revision(engine) == AFTER
    assert failed_at_column(engine) == ("timestamp with time zone", "YES")
    assert pending_index(engine)[1] == NEW_PREDICATE
