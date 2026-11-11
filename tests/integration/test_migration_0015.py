"""The shared query state tables are additive and reversible."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

TABLES = ("query_cursors", "read_replays")


@pytest.mark.parametrize("normalized_database", ["0014"], indirect=True)
def test_shared_state_tables_upgrade_and_downgrade(normalized_database):
    engine = create_engine(normalized_database)
    exists = text("SELECT to_regclass(:name)")
    try:
        with engine.connect() as connection:
            assert all(connection.execute(exists, {"name": t}).scalar() is None for t in TABLES)
        config = Config("alembic.ini")
        command.upgrade(config, "0015")
        with engine.begin() as connection:
            assert all(connection.execute(exists, {"name": t}).scalar() for t in TABLES)
            connection.execute(
                text(
                    "INSERT INTO query_cursors (token, scope, payload, expires_at) "
                    "VALUES (repeat('a', 48), '[]', '{}', now())"
                )
            )
            with pytest.raises(Exception, match="token_length"):
                connection.execute(
                    text(
                        "INSERT INTO query_cursors (token, scope, payload, expires_at) "
                        "VALUES ('short', '[]', '{}', now())"
                    )
                )
        command.downgrade(config, "0014")
        with engine.connect() as connection:
            assert all(connection.execute(exists, {"name": t}).scalar() is None for t in TABLES)
    finally:
        engine.dispose()
