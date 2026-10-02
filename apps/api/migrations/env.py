"""Alembic environment: one history for every module; each module owns a PostgreSQL schema.

The URL comes from Settings (`DATABASE_URL`); pass `-x test=true` to target
`DATABASE_URL_TEST` instead (used by `just db-migrate --test` and integration tests).
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from fragancia_api.config import DatabaseSettings
from fragancia_api.container import metadata

VERSION_SCHEMA = "platform"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    explicit = config.attributes.get("database_url")
    if explicit:
        return str(explicit)
    settings = DatabaseSettings()
    if context.get_x_argument(as_dictionary=True).get("test") == "true":
        return settings.for_tests().database_url
    return settings.database_url


def _include_name(name: str | None, type_: str, _: object) -> bool:
    # Only manage our schemas; never touch `public` or anything Alembic does not own.
    if type_ == "schema":
        return name in {table.schema for table in metadata.tables.values()}
    return True


def _run(connection: Connection) -> None:
    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{VERSION_SCHEMA}"'))
    context.configure(
        connection=connection,
        target_metadata=metadata,
        include_schemas=True,
        include_name=_include_name,
        version_table_schema=VERSION_SCHEMA,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
asyncio.run(_run_online())
