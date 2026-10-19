import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import create_engine


@pytest.mark.integration
def test_postgis_connection_and_spatial_query() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run the PostgreSQL/PostGIS integration test")

    async def check_database() -> None:
        engine = create_engine(Settings(_env_file=None, database_url=url))
        try:
            async with engine.connect() as connection:
                version = await connection.scalar(text("SELECT PostGIS_Version()"))
                distance = await connection.scalar(
                    text(
                        "SELECT ST_Distance("
                        "ST_SetSRID(ST_MakePoint(126.978, 37.5665), 4326)::geography, "
                        "ST_SetSRID(ST_MakePoint(126.978, 37.5675), 4326)::geography)"
                    )
                )
                assert version
                assert distance is not None and 100 < distance < 120
        finally:
            await engine.dispose()

    asyncio.run(check_database())


@pytest.mark.integration
def test_migrations_match_database_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run the PostgreSQL/PostGIS integration test")

    monkeypatch.setenv("APP_DATABASE_URL", url)
    # Includes a regression check: PostGIS extension tables must not appear as
    # application tables to remove, even with tiger/topology in DB search_path.
    command.check(Config("alembic.ini"))
