import asyncio
from logging.config import fileConfig
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.models import Base

# app.models imports all registered models into Base.metadata.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=alembic_helpers.include_object,
        render_item=alembic_helpers.render_item,
        process_revision_directives=alembic_helpers.writer,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=alembic_helpers.include_object,
        render_item=alembic_helpers.render_item,
        process_revision_directives=alembic_helpers.writer,
    )
    # Separate release jobs/developers must not execute DDL concurrently.
    locked = connection.scalar(text("SELECT pg_try_advisory_lock(hashtext('seoul_now:alembic'))"))
    connection.commit()
    if not locked:
        raise RuntimeError("Another migration is running; retry after it completes")
    try:
        # 0009's immutable conversion briefly writes archive files. Fresh-to-head
        # upgrades remove those tables in 0012, so no runtime file store is needed.
        explicit = config.attributes.get("payload_storage_dir")
        with TemporaryDirectory(prefix="seoul-migration-") as temporary:
            if explicit is None:
                config.attributes["payload_storage_dir"] = temporary
            try:
                with context.begin_transaction():
                    context.run_migrations()
                    if explicit is None and any(Path(temporary).rglob("*.blob")):
                        if connection.dialect.has_table(connection, "raw_payloads"):
                            raise RuntimeError(
                                "Historical targets before 0012 require an explicit "
                                "Config.attributes['payload_storage_dir']; "
                                "upgrade to head otherwise"
                            )
            finally:
                if explicit is None:
                    config.attributes.pop("payload_storage_dir", None)
    finally:
        connection.rollback()
        connection.execute(text("SELECT pg_advisory_unlock(hashtext('seoul_now:alembic'))"))
        connection.commit()


async def run_migrations_online() -> None:
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        poolclass=pool.NullPool,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            # PostGIS adds tiger/topology to search_path. Reflect only app tables
            # in public so autogenerate cannot propose dropping extension tables.
            "options": "-csearch_path=public",
        },
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
