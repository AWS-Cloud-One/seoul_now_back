"""Recommendation-only receipts use the same version; downgrade remains guarded."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("normalized_database", ["0017"], indirect=True)
def test_recommend_only_constraint_upgrade_matches_metadata(normalized_database):
    engine = create_engine(normalized_database)
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "0018")
        with engine.connect() as connection:
            constraint = connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_chat_run_commits_version'"
                )
            )
            assert "current_version >= previous_version" in constraint
        command.downgrade(config, "0017")
        command.upgrade(config, "0018")
    finally:
        engine.dispose()
