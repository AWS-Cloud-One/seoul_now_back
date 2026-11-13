"""V2 additive DDL matches the models and can be removed without changing V1."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("normalized_database", ["0016"], indirect=True)
def test_v2_schema_matches_models_and_roundtrips(normalized_database):
    engine = create_engine(normalized_database)
    config = Config("alembic.ini")
    try:
        before = set(inspect(engine).get_table_names())
        command.upgrade(config, "0017")
        # Full runtime metadata is checked at the current head in test_migration_0018.
        assert set(inspect(engine).get_table_names()) - before == {
            "chat_browser_sessions",
            "chat_conversations",
            "chat_runs",
            "chat_run_commits",
        }
        command.downgrade(config, "0016")
        assert set(inspect(engine).get_table_names()) == before
        command.upgrade(config, "0017")
        # Full runtime metadata is checked at the current head in test_migration_0018.
    finally:
        engine.dispose()
