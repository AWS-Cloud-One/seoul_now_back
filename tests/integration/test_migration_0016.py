"""Shared trip state tables are additive, reversible and match the runtime models."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

TABLES = ("trip_progress", "trip_requests", "origin_contexts")


@pytest.mark.parametrize("normalized_database", ["0015"], indirect=True)
def test_trip_state_tables_upgrade_downgrade_and_match_models(normalized_database):
    engine = create_engine(normalized_database)
    exists = text("SELECT to_regclass(:name)")
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0016")
        # Models and frozen DDL agree (indexes, defaults, constraints).
        command.check(config)
        with engine.begin() as connection:
            assert all(connection.execute(exists, {"name": t}).scalar() for t in TABLES)
            with pytest.raises(Exception, match="lock_pair"):
                connection.execute(
                    text(
                        "INSERT INTO trip_progress (trip_id, expires_at, lock_owner) "
                        "VALUES (gen_random_uuid(), now(), gen_random_uuid())"
                    )
                )
        insert = text(
            "INSERT INTO origin_contexts (token, trip_id, draft, is_latest, expires_at) "
            "VALUES (:k, :t, '{}', true, now())"
        )
        with engine.begin() as connection:
            trip = connection.execute(text("SELECT gen_random_uuid()")).scalar()
            connection.execute(insert, {"k": "one", "t": trip})
            # Only one draft per trip can be the latest.
            with pytest.raises(Exception, match="uq_origin_contexts_latest"):
                connection.execute(insert, {"k": "two", "t": trip})
        command.downgrade(config, "0015")
        with engine.connect() as connection:
            assert all(connection.execute(exists, {"name": t}).scalar() is None for t in TABLES)
    finally:
        engine.dispose()
