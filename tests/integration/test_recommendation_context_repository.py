"""Historical result writes/reads against an isolated migrated PostGIS database."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.dependencies import get_write_session
from app.core.config import Settings
from app.db.session import read_session_factory, write_session_factory
from app.main import create_app
from app.models import (
    ChatConditionAgeGroup,
    ChatConditionPurpose,
    ChatConditionVersion,
    ChatSession,
    Place,
    RecommendationCard,
    RecommendationEvidence,
    RecommendationResult,
)
from app.repositories.recommendation_context import (
    ConditionVersionMismatch,
    ConditionVersionNotFound,
    RecommendationContextRepository,
    RecommendationSnapshot,
)
from app.schemas.location import Coordinates, Location
from app.schemas.recommendations import Conditions, Purpose

pytestmark = pytest.mark.integration


@pytest.fixture
def snapshot():
    now, result_id = datetime.now(UTC), uuid4()
    result = RecommendationResult(
        result_id=result_id,
        trip_id=uuid4(),
        request_id=uuid4(),
        condition_version=0,
        kind="recommendation",
        routes_status="ok",
        explanation_status="fallback",
        created_at=now,
        context_expires_at=now + timedelta(minutes=30),
        context_token_digest="a" * 64,
    )
    conditions = Conditions(
        origin=Location(
            label="서울역",
            coordinates=Coordinates(latitude=37.5547, longitude=126.9706),
            address="서울특별시 용산구",
            region="seoul",
            supported=True,
        ),
        purposes=[Purpose(purpose="cafe", keyword="조용한"), Purpose(purpose="walk")],
        group_type="friends",
        age_groups=["thirties", "twenties"],
        headcount=2,
        max_travel_seconds=1800,
    )
    cards, evidence = [], []
    for position, place_id in enumerate(("kakao:z", "kakao:a")):
        cards.append(
            RecommendationCard(
                result_id=result_id,
                place_id=place_id,
                result_kind="recommendation",
                position=position,
                rank=position + 1,
                place_name=f"당시 카페 {position}",
                purpose="cafe",
                place_location="SRID=4326;POINT(127 37.5)",
                place_freshness="unknown",
                place_collected_at=now,
                place_source="kakao_local",
                duration_seconds=600 + position,
                distance_meters=Decimal("1234.5"),
                route_status="available",
                route_freshness="fresh",
                route_collected_at=now,
                route_source="kakao_mobility",
                parking_status="available",
                parking_checked_at=now,
                parking_search_radius_meters=300,
                parking_nearest_distance_meters=Decimal("95.25"),
                parking_total_count=2,
                reason_text="당시 확인한 이동시간과 등록 주차장 거리",
                reason_source="fallback",
            )
        )
        for index, kind in enumerate(("eta", "parking")):
            evidence.append(
                RecommendationEvidence(
                    result_id=result_id,
                    place_id=place_id,
                    result_kind="recommendation",
                    position=index,
                    type=kind,
                    text=f"당시 {kind} 근거",
                    duration_seconds=600 + position if kind == "eta" else None,
                    distance_meters=Decimal("1234.5") if kind == "eta" else Decimal("95.25"),
                    freshness="fresh" if kind == "eta" else "unknown",
                    collected_at=now if kind == "eta" else None,
                    source="kakao_mobility" if kind == "eta" else None,
                )
            )
    return RecommendationSnapshot(
        result, conditions, tuple(reversed(cards)), tuple(reversed(evidence))
    )


@asynccontextmanager
async def database(url):
    engine = create_async_engine(url)
    try:
        yield write_session_factory(engine), read_session_factory(engine)
    finally:
        await engine.dispose()


async def seed_conditions(session, snapshot, *, version=0, new_trip=True):
    trip_id, conditions = snapshot.result.trip_id, snapshot.conditions
    if new_trip:
        await session.execute(ChatSession.__table__.insert().values(trip_id=trip_id))
    origin = conditions.origin
    await session.execute(
        ChatConditionVersion.__table__.insert().values(
            trip_id=trip_id,
            version=version,
            origin_label=origin.label if origin else None,
            origin_location=(
                f"SRID=4326;POINT({origin.coordinates.longitude} {origin.coordinates.latitude})"
                if origin
                else None
            ),
            origin_address=origin.address if origin else None,
            origin_region=origin.region if origin else None,
            origin_supported=origin.supported if origin else None,
            group_type=conditions.group_type,
            headcount=conditions.headcount,
            max_travel_seconds=conditions.max_travel_seconds,
        )
    )
    for position, purpose in enumerate(conditions.purposes):
        await session.execute(
            ChatConditionPurpose.__table__.insert().values(
                trip_id=trip_id,
                condition_version=version,
                position=position,
                purpose=purpose.purpose,
                keyword=purpose.keyword,
            )
        )
    for position, age in enumerate(conditions.age_groups or []):
        await session.execute(
            ChatConditionAgeGroup.__table__.insert().values(
                trip_id=trip_id,
                condition_version=version,
                position=position,
                age_group=age,
            )
        )


async def assert_no_results(session):
    for model in (RecommendationResult, RecommendationCard, RecommendationEvidence):
        assert await session.scalar(select(func.count()).select_from(model)) == 0


def test_round_trip_preserves_history_order_and_returns_untracked_copies(
    normalized_database, snapshot
):
    async def run():
        async with database(normalized_database) as (writers, readers):
            async with writers.begin() as session:
                await seed_conditions(session, snapshot)
                await RecommendationContextRepository(session).save(snapshot)
            # A later condition version and latest place cache must not change history.
            async with writers.begin() as session:
                later = replace(
                    snapshot, conditions=snapshot.conditions.model_copy(update={"headcount": 4})
                )
                await seed_conditions(session, later, version=1, new_trip=False)
                await session.execute(
                    update(ChatSession)
                    .where(ChatSession.trip_id == snapshot.result.trip_id)
                    .values(condition_version=1)
                )
                await session.execute(
                    Place.__table__.insert().values(
                        place_id="kakao:z",
                        name="새 장소 이름",
                        location="SRID=4326;POINT(128 38)",
                        collected_at=datetime.now(UTC),
                        expires_at=datetime.now(UTC) + timedelta(days=1),
                    )
                )
            async with readers() as session:
                repo = RecommendationContextRepository(session)
                stored = await repo.get(snapshot.result.trip_id, snapshot.result.result_id)
                assert stored.conditions == snapshot.conditions
                assert stored.result.created_at == snapshot.result.created_at
                assert stored.result.context_token_digest == "a" * 64
                assert [c.place_id for c in stored.cards] == ["kakao:z", "kakao:a"]
                assert [e.type for e in stored.evidence] == ["eta", "parking", "eta", "parking"]
                card = stored.cards[0]
                assert card.place_name == "당시 카페 0"
                assert card.distance_meters == Decimal("1234.5")
                assert card.place_observed_at is None and card.route_observed_at is None
                assert stored.evidence[1].source is None and stored.evidence[1].observed_at is None
                assert stored.evidence[1].distance_meters == Decimal("95.25")
                assert await repo.get(uuid4(), snapshot.result.result_id) is None
                assert await repo.get(snapshot.result.trip_id, uuid4()) is None
                assert not session.identity_map
            # Editing returned data and committing the session cannot overwrite history.
            async with writers.begin() as session:
                repo = RecommendationContextRepository(session)
                stored = await repo.get(snapshot.result.trip_id, snapshot.result.result_id)
                stored.cards[0].place_name = "변경 시도"
                assert not session.dirty
            async with readers() as session:
                stored = await RecommendationContextRepository(session).get(
                    snapshot.result.trip_id, snapshot.result.result_id
                )
                assert stored.cards[0].place_name == "당시 카페 0"

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["trip", "version"])
def test_missing_conditions_are_not_created(normalized_database, snapshot, missing):
    async def run():
        async with database(normalized_database) as (writers, _):
            async with writers.begin() as session:
                if missing == "version":
                    await seed_conditions(session, snapshot)
                    snapshot.result.condition_version = 1
                with pytest.raises(ConditionVersionNotFound):
                    await RecommendationContextRepository(session).save(snapshot)
                await assert_no_results(session)
                count = await session.scalar(select(func.count()).select_from(ChatConditionVersion))
                assert count == int(missing == "version")

    asyncio.run(run())


@pytest.mark.parametrize(
    "field", ["origin", "purposes", "age_groups", "headcount", "max_travel_seconds"]
)
def test_existing_condition_values_cannot_be_overwritten(normalized_database, snapshot, field):
    async def run():
        async with database(normalized_database) as (writers, _):
            async with writers.begin() as session:
                await seed_conditions(session, snapshot)
                changed = snapshot.conditions.model_copy(deep=True)
                if field == "origin":
                    changed.origin.coordinates.latitude += 0.001
                elif field == "purposes":
                    changed.purposes[0].keyword = "다른 키워드"
                elif field == "age_groups":
                    changed.age_groups.reverse()
                else:
                    setattr(changed, field, getattr(changed, field) + 1)
                repo = RecommendationContextRepository(session)
                with pytest.raises(ConditionVersionMismatch):
                    await repo.save(replace(snapshot, conditions=changed))
                await assert_no_results(session)
                # The original version is still accepted after the failed write.
                await repo.save(snapshot)

    asyncio.run(run())


def test_child_failure_rolls_back_whole_aggregate_and_session_remains_usable(
    normalized_database, snapshot
):
    async def run():
        async with database(normalized_database) as (writers, readers):
            async with writers.begin() as session:
                await seed_conditions(session, snapshot)
                repo = RecommendationContextRepository(session)
                original = snapshot.evidence[0].text
                snapshot.evidence[0].text = " "  # Fails after result and cards have been inserted.
                with pytest.raises(IntegrityError):
                    await repo.save(snapshot)
                await assert_no_results(session)
                snapshot.evidence[0].text = original
                await repo.save(snapshot)
            async with readers() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(RecommendationCard)) == 2
                )

    asyncio.run(run())


def test_repository_never_commits_and_duplicate_save_never_overwrites(
    normalized_database, snapshot
):
    async def run():
        async with database(normalized_database) as (writers, readers):
            async with writers() as session:
                repo = RecommendationContextRepository(session)
                with pytest.raises(InvalidRequestError, match="caller-owned"):
                    await repo.save(snapshot)
                async with session.begin():
                    await seed_conditions(session, snapshot)
                    await repo.save(snapshot)
                    async with readers() as other:
                        await assert_no_results(other)
                    snapshot.cards[0].place_name = "덮어쓰기 시도"
                    with pytest.raises(IntegrityError):
                        await repo.save(snapshot)
            async with readers() as session:
                stored = await RecommendationContextRepository(session).get(
                    snapshot.result.trip_id, snapshot.result.result_id
                )
                assert stored.cards[1].place_name == "당시 카페 1"

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid", ["empty", "gap", "foreign_card", "foreign_evidence", "evidence_gap"]
)
def test_invalid_aggregate_membership_leaves_no_result(normalized_database, snapshot, invalid):
    async def run():
        async with database(normalized_database) as (writers, _):
            async with writers.begin() as session:
                await seed_conditions(session, snapshot)
                candidate = snapshot
                if invalid == "empty":
                    candidate = replace(snapshot, cards=())
                elif invalid == "gap":
                    snapshot.cards[0].position = 2
                elif invalid == "foreign_card":
                    snapshot.cards[0].result_id = uuid4()
                elif invalid == "foreign_evidence":
                    snapshot.evidence[0].place_id = "kakao:other"
                else:
                    snapshot.evidence[0].position = 5
                with pytest.raises(ValueError):
                    await RecommendationContextRepository(session).save(candidate)
                await assert_no_results(session)

    asyncio.run(run())


def test_general_candidates_and_unspecified_conditions_round_trip(normalized_database, snapshot):
    snapshot.result.kind = "general_candidates"
    snapshot.result.routes_status = "failed"
    snapshot.result.explanation_status = "skipped"
    conditions = snapshot.conditions.model_copy(update={"origin": None, "age_groups": None})
    for card in snapshot.cards:
        card.result_kind = "general_candidates"
        card.rank = card.duration_seconds = card.distance_meters = None
        card.route_status = "error"
        card.reason_source = "none"
    snapshot = replace(snapshot, conditions=conditions, evidence=())

    async def run():
        async with database(normalized_database) as (writers, readers):
            async with writers.begin() as session:
                await seed_conditions(session, snapshot)
                await RecommendationContextRepository(session).save(snapshot)
            async with readers() as session:
                stored = await RecommendationContextRepository(session).get(
                    snapshot.result.trip_id, snapshot.result.result_id
                )
                assert stored.conditions == conditions
                assert stored.result.kind == "general_candidates" and stored.evidence == ()
                assert all(c.duration_seconds is None and c.rank is None for c in stored.cards)

    asyncio.run(run())


def test_api_writer_rolls_back_uncommitted_work_and_reader_stays_read_only(
    normalized_database, snapshot
):
    async def run():
        app = create_app(Settings(_env_file=None, database_url=normalized_database))
        async with app.router.lifespan_context(app):
            request = SimpleNamespace(app=app)
            async for session in get_write_session(request):
                assert await session.scalar(text("SHOW transaction_read_only")) == "off"
                await seed_conditions(session, snapshot)
                await RecommendationContextRepository(session).save(snapshot)
                # Closing the dependency without an explicit commit must discard all writes.
            async with app.state.session_factory() as reader:
                await assert_no_results(reader)
                assert await reader.scalar(text("SHOW transaction_read_only")) == "on"
                with pytest.raises(DBAPIError) as error:
                    await reader.execute(update(ChatSession).values(condition_version=0))
                assert error.value.orig.sqlstate == "25006"
            async for session in get_write_session(request):
                async with session.begin():
                    await seed_conditions(session, snapshot)
                    await RecommendationContextRepository(session).save(snapshot)
            async with app.state.session_factory() as reader:
                assert (
                    await reader.scalar(select(func.count()).select_from(RecommendationResult)) == 1
                )

    asyncio.run(run())
