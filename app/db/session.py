from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    # Creating an engine does not open a database connection.
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=settings.db_connect_timeout_seconds,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
    )


def read_session_factory(engine: AsyncEngine):
    """One committed snapshot for every SELECT in an API request; writers use the base engine."""
    return async_sessionmaker(
        engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True),
        expire_on_commit=False,
        autoflush=False,
    )


def write_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Share the engine/pool; the caller owns the short write transaction and commit."""
    return async_sessionmaker(
        engine.execution_options(isolation_level="READ COMMITTED", postgresql_readonly=False),
        expire_on_commit=False,
        autoflush=False,
    )
