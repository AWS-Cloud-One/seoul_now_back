import asyncio
import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.dependencies import get_context_reader, get_context_writer
from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.db.session import read_session_factory, write_session_factory
from app.models import RecommendationCard, RecommendationResult
from app.repositories.recommendation_context import RecommendationContextRepository
from app.services.recommendation_context import RecommendationContextService
from tests.fixtures.recommendation_context import NOW, response, seed_conditions

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("general", [False, True])
def test_registration_and_token_only_resolution_on_real_storage(normalized_database, general):
    async def run():
        engine = create_async_engine(normalized_database)
        readers, writers = read_session_factory(engine), write_session_factory(engine)
        settings = Settings(_env_file=None, result_context_ttl_seconds=60)
        data = response(general=general)
        try:
            async with writers.begin() as session:
                await seed_conditions(session, data)
                service = get_context_writer(session, settings)
                service.clock = lambda: NOW
                registered = await service.register(uuid4(), data)
            token = registered.result["context_token"]
            async with readers() as session:
                service = get_context_reader(session, settings)
                service.clock = lambda: NOW + timedelta(seconds=59)
                stored = await service.resolve(token, place_id="kakao:original")
                assert stored.result.trip_id == data.trip_id
                assert (
                    stored.result.context_token_digest == hashlib.sha256(token.encode()).hexdigest()
                )
                assert stored.result.created_at == NOW
                assert stored.result.context_expires_at == NOW + timedelta(seconds=60)
                assert stored.conditions == data.condition_update.conditions
                assert stored.cards[0].address == "당시 주소"
                assert stored.cards[0].route_observed_at is None
                if general:
                    assert stored.cards[0].rank is None and stored.evidence == ()
                else:
                    assert stored.cards[0].duration_seconds == 600
                    assert [e.type for e in stored.evidence] == ["eta", "parking"]
                with pytest.raises(ServiceError) as wrong:
                    await service.resolve(token, trip_id=uuid4())
                assert wrong.value.code == "INVALID_CONTEXT"
                service.clock = lambda: NOW + timedelta(seconds=60)
                with pytest.raises(ServiceError) as expired:
                    await service.resolve(token)
                assert expired.value.code == "CONTEXT_EXPIRED"
                # Expiry is access policy; it neither deletes history nor refreshes the token.
                assert (
                    await session.scalar(select(func.count()).select_from(RecommendationResult))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", [False, True])
def test_condition_owner_errors_are_translated_without_partial_registration(
    normalized_database, mismatch
):
    async def run():
        engine = create_async_engine(normalized_database)
        data = response()
        try:
            async with write_session_factory(engine).begin() as session:
                if mismatch:
                    await seed_conditions(session, data)
                    data.condition_update.conditions.headcount += 1
                    data.result["conditions"]["headcount"] += 1
                service = RecommendationContextService(
                    RecommendationContextRepository(session), clock=lambda: NOW
                )
                with pytest.raises(ServiceError) as error:
                    await service.register(uuid4(), data)
                assert error.value.code == (
                    "CONTEXT_CONDITIONS_MISMATCH" if mismatch else "DATA_UNAVAILABLE"
                )
                assert (
                    await session.scalar(select(func.count()).select_from(RecommendationResult))
                    == 0
                )
                assert (
                    await session.scalar(select(func.count()).select_from(RecommendationCard)) == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_caller_rollback_discards_registration_and_retry_keeps_source(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        readers, writers = read_session_factory(engine), write_session_factory(engine)
        data = response(general=True)
        try:
            with pytest.raises(RuntimeError):
                async with writers.begin() as session:
                    await seed_conditions(session, data)
                    service = RecommendationContextService(
                        RecommendationContextRepository(session), clock=lambda: NOW
                    )
                    discarded = await service.register(uuid4(), data)
                    raise RuntimeError("later work failed")
            async with readers() as session:
                service = RecommendationContextService(
                    RecommendationContextRepository(session), clock=lambda: NOW
                )
                with pytest.raises(ServiceError) as error:
                    await service.resolve(discarded.result["context_token"])
                assert error.value.code == "INVALID_CONTEXT"
            async with writers.begin() as session:
                await seed_conditions(session, data)
                service = RecommendationContextService(
                    RecommendationContextRepository(session), clock=lambda: NOW
                )
                original = await service.register(uuid4(), data)
                retry = response()
                retry.trip_id = data.trip_id
                retry.condition_update.version = 1
                await seed_conditions(session, retry, version=1, new_trip=False)
                registered = await service.register(
                    uuid4(), retry, source_result_id=UUID(data.result["result_id"])
                )
            async with readers() as session:
                service = RecommendationContextService(
                    RecommendationContextRepository(session), clock=lambda: NOW
                )
                new = await service.resolve(registered.result["context_token"])
                old = await service.resolve(original.result["context_token"])
                assert new.result.source_result_id == old.result.result_id
                assert new.result.condition_version == 1 and old.result.condition_version == 0
                assert new.conditions == old.conditions
                assert old.cards[0].rank is None and new.cards[0].rank == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
