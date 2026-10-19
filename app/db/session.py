from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
