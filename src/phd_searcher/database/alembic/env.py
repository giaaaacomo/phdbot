"""Alembic environment — async, single-schema Postgres.

The DB URL and schema are read straight from the service's env vars (same names
pydantic-settings uses), so migrations need no extra config. Models are pulled in
via `phd_searcher.database.models` so autogenerate sees whatever is registered on Base.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import phd_searcher.database.models  # noqa: F401  # register models on Base.metadata
from phd_searcher.database.models.base import Base

load_dotenv()
config = context.config

DB_URL = os.environ["PHD_SEARCHER__DATABASE__URL"]
SCHEMA = os.environ.get("PHD_SEARCHER__DATABASE__SCHEMA_NAME", "public")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_object(obj: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    # version_table_schema="public" vs default-schema reflection (None) confuses
    # Alembic's builtin exclusion; exclude the version table explicitly.
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    connection = connection.execution_options(schema_translate_map={None: SCHEMA})
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = DB_URL
    connectable = async_engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        await connection.commit()
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
