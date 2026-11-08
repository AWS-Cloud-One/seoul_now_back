"""The service scaffold can store real relational rows before API wiring exists."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command

from app.models import Base
from tests.integration.test_migration_0009 import config

pytestmark = pytest.mark.integration
NEW_TABLES = {
    "place_categories",
    "places",
    "chat_sessions",
    "chat_messages",
    "chat_condition_versions",
    "chat_condition_purposes",
    "chat_condition_age_groups",
    "chat_excluded_places",
}


def table(name):
    return Base.metadata.tables[name]


def insert(c, table_name, **values):
    c.execute(table(table_name).insert().values(**values))


def rejects(c, statement):
    with pytest.raises(sa.exc.IntegrityError), c.begin_nested():
        c.execute(statement)


def create_session(c, trip_id):
    insert(c, "chat_sessions", trip_id=trip_id)
    insert(c, "chat_condition_versions", trip_id=trip_id, version=0)


def test_service_foundations_start_empty(normalized_database):
    engine = sa.create_engine(normalized_database)
    with engine.connect() as c:
        for name in NEW_TABLES:
            assert c.scalar(sa.select(sa.func.count()).select_from(table(name))) == 0
    engine.dispose()


def test_conditions_history_places_and_messages_keep_their_relationships(normalized_database):
    engine = sa.create_engine(normalized_database)
    trip, other_trip, request_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    with engine.begin() as c:
        insert(c, "place_categories", category_code="CE7", name="카페")
        place = dict(
            place_id="kakao:123",
            name="서울 카페",
            category_code="CE7",
            location="SRID=4326;POINT(127 37.5)",
            collected_at=now,
            expires_at=now + timedelta(days=1),
        )
        insert(c, "places", **place)
        rejects(c, table("places").insert().values(**{**place, "place_id": "bad-id"}))
        rejects(
            c,
            table("places")
            .insert()
            .values(
                **{
                    **place,
                    "place_id": "kakao:expired",
                    "expires_at": now,
                }
            ),
        )
        rejects(
            c,
            table("places")
            .insert()
            .values(
                **{
                    **place,
                    "place_id": "kakao:missing-category",
                    "category_code": "missing",
                }
            ),
        )
        create_session(c, trip)
        create_session(c, other_trip)
        insert(
            c,
            "chat_condition_versions",
            trip_id=trip,
            version=1,
            origin_label="출발지",
            origin_location="SRID=4326;POINT(127 37.5)",
            origin_region="seoul",
            origin_supported=True,
            group_type="friends",
            headcount=2,
        )
        insert(c, "chat_condition_versions", trip_id=other_trip, version=2)
        c.execute(
            table("chat_sessions")
            .update()
            .where(table("chat_sessions").c.trip_id == trip)
            .values(condition_version=1)
        )
        for position, purpose, keyword in [(0, "cafe", "디저트"), (1, "walk", None)]:
            insert(
                c,
                "chat_condition_purposes",
                trip_id=trip,
                condition_version=1,
                purpose=purpose,
                position=position,
                keyword=keyword,
            )
        for position, age_group in enumerate(["twenties", "thirties"]):
            insert(
                c,
                "chat_condition_age_groups",
                trip_id=trip,
                condition_version=1,
                age_group=age_group,
                position=position,
            )
        insert(c, "chat_excluded_places", trip_id=trip, place_id="kakao:123")
        insert(
            c,
            "chat_messages",
            trip_id=trip,
            sequence=1,
            request_id=request_id,
            role="user",
            content="카페를 찾아줘",
            condition_version=0,
        )
        insert(
            c,
            "chat_messages",
            trip_id=trip,
            sequence=2,
            request_id=request_id,
            role="assistant",
            content="조건을 반영했어",
            kind="no_candidates",
            base_condition_version=0,
            condition_version=1,
            condition_applied=True,
        )
        # A request may have both user and assistant messages, but sequence is unique per trip.
        rejects(
            c,
            table("chat_messages")
            .insert()
            .values(trip_id=trip, sequence=2, role="assistant", content="duplicate"),
        )
        # Version 2 exists only for the other trip; a numeric match must not bypass the FK.
        rejects(
            c,
            table("chat_messages")
            .insert()
            .values(
                trip_id=trip,
                sequence=3,
                role="assistant",
                content="wrong trip",
                condition_version=2,
            ),
        )
        rejects(
            c,
            table("chat_messages")
            .insert()
            .values(
                trip_id=trip,
                sequence=3,
                role="assistant",
                content="no version evidence",
                condition_applied=True,
            ),
        )
        rejects(
            c,
            table("chat_messages")
            .insert()
            .values(
                trip_id=trip,
                sequence=3,
                role="assistant",
                content="unapplied change",
                base_condition_version=0,
                condition_version=1,
                condition_applied=False,
            ),
        )
        rejects(
            c,
            table("chat_condition_purposes")
            .insert()
            .values(trip_id=trip, condition_version=1, purpose="food", position=2),
        )
        rejects(
            c,
            table("chat_condition_purposes")
            .insert()
            .values(trip_id=trip, condition_version=1, purpose="cafe", position=1),
        )
        rejects(
            c,
            table("chat_condition_age_groups")
            .insert()
            .values(trip_id=trip, condition_version=1, age_group="twenties", position=2),
        )
        rejects(
            c, table("chat_excluded_places").insert().values(trip_id=trip, place_id="kakao:123")
        )
        rejects(
            c, table("chat_excluded_places").insert().values(trip_id=trip, place_id="kakao:unknown")
        )
        rejects(c, table("places").delete().where(table("places").c.place_id == "kakao:123"))
        rejects(
            c,
            table("chat_condition_versions")
            .delete()
            .where(
                table("chat_condition_versions").c.trip_id == trip,
                table("chat_condition_versions").c.version == 0,
            ),
        )
    with engine.connect() as c:
        versions = c.execute(
            sa.select(table("chat_messages").c.condition_version)
            .where(table("chat_messages").c.trip_id == trip)
            .order_by(table("chat_messages").c.sequence)
        )
        assert list(versions.scalars()) == [0, 1]
        initial = (
            c.execute(
                sa.select(table("chat_condition_versions")).where(
                    table("chat_condition_versions").c.trip_id == trip,
                    table("chat_condition_versions").c.version == 0,
                )
            )
            .mappings()
            .one()
        )
        assert initial.origin_location is None and initial.headcount is None
        assert (
            c.execute(
                sa.select(table("chat_sessions").c.condition_version).where(
                    table("chat_sessions").c.trip_id == trip
                )
            ).scalar_one()
            == 1
        )
    # Data-bearing foundations cannot be dropped by a routine downgrade.
    with pytest.raises(RuntimeError, match="cannot be reconstructed"):
        command.downgrade(config(), "0008")
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0013"
        assert c.scalar(sa.select(sa.func.count()).select_from(table("chat_messages"))) == 2
    engine.dispose()


def test_session_current_version_must_exist_at_commit(normalized_database):
    engine = sa.create_engine(normalized_database)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as c:
        insert(c, "chat_sessions", trip_id=uuid4())
    with engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(table("chat_sessions"))) == 0
    # Explicit cleanup of a session and its condition version must remain possible.
    trip = uuid4()
    with engine.begin() as c:
        create_session(c, trip)
    with engine.begin() as c:
        c.execute(
            table("chat_condition_versions")
            .delete()
            .where(table("chat_condition_versions").c.trip_id == trip)
        )
        c.execute(table("chat_sessions").delete().where(table("chat_sessions").c.trip_id == trip))
    with engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(table("chat_sessions"))) == 0
    engine.dispose()
