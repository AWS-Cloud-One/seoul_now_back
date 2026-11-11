"""Frozen 0013 DDL preserves old rows and refuses destructive rollback."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command

from app.models import Base
from tests.fixtures.migrations import current_head
from tests.integration.test_migration_0009 import config
from tests.integration.test_migration_0009 import migration_database as migration_database
from tests.integration.test_migration_0010 import fail_upgrade_at
from tests.integration.test_model_constraints import snapshot_card

pytestmark = pytest.mark.integration
NEW_TABLES = {
    "recommendation_results",
    "recommendation_cards",
    "recommendation_evidence",
    "recommendation_search_areas",
}


def table(name):
    return Base.metadata.tables[name]


def seed_existing(engine):
    command.upgrade(config(), "0012")
    trip = uuid4()
    with engine.begin() as connection:
        connection.execute(table("chat_sessions").insert().values(trip_id=trip))
        connection.execute(
            table("chat_condition_versions")
            .insert()
            .values(
                trip_id=trip,
                version=0,
                headcount=2,
            )
        )
        connection.execute(
            table("chat_condition_purposes")
            .insert()
            .values(
                trip_id=trip,
                condition_version=0,
                purpose="cafe",
                position=0,
            )
        )
        connection.execute(
            table("parking_facilities")
            .insert()
            .values(
                parking_id=uuid4(),
                name="existing",
                location="SRID=4326;POINT(127 37.5)",
            )
        )
    return trip


def existing_rows(engine):
    with engine.connect() as connection:
        return {
            name: connection.execute(sa.select(table(name))).all()
            for name in (
                "chat_sessions",
                "chat_condition_versions",
                "chat_condition_purposes",
                "parking_facilities",
            )
        }


def test_0013_upgrade_preserves_data_and_rolls_back_failed_ddl(migration_database):
    engine = migration_database
    seed_existing(engine)
    before = existing_rows(engine)
    fail_upgrade_at("CREATE TABLE recommendation_cards")
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0012"
        )
        assert not NEW_TABLES & set(sa.inspect(connection).get_table_names())
    assert existing_rows(engine) == before
    command.upgrade(config(), "head")
    command.upgrade(config(), "head")
    command.check(config())
    assert existing_rows(engine) == before
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == current_head()
        )
        assert set(sa.inspect(connection).get_table_names()) - {
            "alembic_version",
            "spatial_ref_sys",
        } == set(Base.metadata.tables)
        for name in NEW_TABLES:
            assert connection.scalar(sa.select(sa.func.count()).select_from(table(name))) == 0


def test_0013_empty_downgrade_preserves_old_data_and_can_upgrade_again(migration_database):
    engine = migration_database
    seed_existing(engine)
    before = existing_rows(engine)
    command.upgrade(config(), "head")
    command.downgrade(config(), "0012")
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0012"
        )
        assert not NEW_TABLES & set(sa.inspect(connection).get_table_names())
    assert existing_rows(engine) == before
    command.upgrade(config(), "head")
    command.check(config())


def test_0013_downgrade_refuses_to_discard_saved_context(migration_database):
    engine = migration_database
    trip = seed_existing(engine)
    command.upgrade(config(), "head")
    now = datetime.now(UTC)
    result = dict(
        result_id=uuid4(),
        trip_id=trip,
        condition_version=0,
        request_id=uuid4(),
        kind="recommendation",
        routes_status="ok",
        explanation_status="fallback",
        created_at=now,
        context_expires_at=now + timedelta(minutes=30),
        context_token_digest="a" * 64,
    )
    with engine.begin() as connection:
        connection.execute(table("recommendation_results").insert().values(**result))
        connection.execute(table("recommendation_cards").insert().values(**snapshot_card(result)))
        connection.execute(
            table("recommendation_evidence")
            .insert()
            .values(
                result_id=result["result_id"],
                place_id="kakao:place",
                position=0,
                result_kind="recommendation",
                type="eta",
                text="자동차 이동시간 600초",
                duration_seconds=600,
                distance_meters=1000,
                freshness="fresh",
                source="kakao_mobility",
                collected_at=now,
            )
        )
    with pytest.raises(RuntimeError, match="refuses to discard recommendation context"):
        command.downgrade(config(), "0012")
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == current_head()
        )
        for name in NEW_TABLES:
            expected = 0 if name == "recommendation_search_areas" else 1
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(table(name))) == expected
            )
    command.check(config())
