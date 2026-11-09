import asyncio
import hashlib
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.repositories.recommendation_context import (
    ConditionVersionMismatch,
    ConditionVersionNotFound,
    RecommendationContextRepository,
)
from app.schemas.pending import RetryRoutesRequest
from app.services.recommendation_context import RecommendationContextService
from tests.fixtures.recommendation_context import NOW, response


def repository():
    repo = AsyncMock(spec=RecommendationContextRepository)
    repo.get.return_value = repo.get_by_token_digest.return_value = None
    return repo


async def issued(*, general=False, ttl=1800):
    repo = repository()
    service = RecommendationContextService(repo, ttl_seconds=ttl, clock=lambda: NOW)
    data = response(general=general)
    registered = await service.register(uuid4(), data)
    stored = repo.save.call_args.args[0]
    repo.get_by_token_digest.return_value = stored
    return service, repo, data, registered, stored


def test_register_preserves_original_facts_and_only_stores_token_digest():
    async def run():
        service, repo, original, registered, stored = await issued(ttl=60)
        assert original.result["context_token"] is None
        assert original.result["context_expires_at"] is None
        token = registered.result["context_token"]
        assert len(token) == 43
        assert stored.result.context_token_digest == hashlib.sha256(token.encode()).hexdigest()
        assert stored.result.context_expires_at == NOW + timedelta(seconds=60)
        assert stored.result.created_at == NOW
        assert stored.cards[0].place_name == "당시 카페"
        assert stored.cards[0].place_location == "SRID=4326;POINT(126.98 37.57)"
        assert stored.cards[0].distance_meters == Decimal("1234.5")
        assert stored.cards[0].route_observed_at is None
        assert stored.evidence[1].source is None and stored.evidence[1].collected_at is None
        assert stored.evidence[1].distance_meters == Decimal("95.25")
        registered.result["cards"][0]["place"]["value"]["name"] = "호출부 변경"
        assert original.result["cards"][0]["place"]["value"]["name"] == "당시 카페"
        assert stored.cards[0].place_name == "당시 카페"
        assert await service.resolve(token, place_id="kakao:original") is stored
        repo.get_by_token_digest.assert_awaited_with(stored.result.context_token_digest)
        other = await service.register(uuid4(), response())
        assert other.result["context_token"] != token

    asyncio.run(run())


@pytest.mark.parametrize(
    "seconds,code", [(59, None), (60, "CONTEXT_EXPIRED"), (61, "CONTEXT_EXPIRED")]
)
def test_expiration_boundary_does_not_refresh_on_access(seconds, code):
    async def run():
        service, repo, original, registered, stored = await issued(ttl=60)
        service.clock = lambda: NOW + timedelta(seconds=seconds)
        args = dict(
            trip_id=original.trip_id,
            result_id=original.result["result_id"],
            place_id="kakao:original",
        )
        if code:
            with pytest.raises(ServiceError) as error:
                await service.resolve(registered.result["context_token"], **args)
            assert error.value.code == code and error.value.retryable is False
        else:
            assert await service.resolve(registered.result["context_token"], **args) is stored
        assert stored.result.context_expires_at == NOW + timedelta(seconds=60)
        repo.save.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("token", [None, "", "a" * 42, "a" * 44, "가" * 43, " " * 43])
def test_malformed_tokens_fail_before_repository_access(token):
    async def run():
        repo = repository()
        with pytest.raises(ServiceError) as error:
            await RecommendationContextService(repo).resolve(token)
        assert error.value.code == "INVALID_CONTEXT"
        repo.get_by_token_digest.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mismatch", ["trip", "result", "place", "digest", "missing", "invalid_uuid"]
)
def test_tampering_or_wrong_membership_is_invalid_even_if_result_expired(mismatch):
    async def run():
        service, repo, _, registered, stored = await issued()
        service.clock = lambda: NOW + timedelta(days=1)
        args = {}
        if mismatch == "trip":
            args["trip_id"] = uuid4()
        elif mismatch == "result":
            args["result_id"] = uuid4()
        elif mismatch == "invalid_uuid":
            args["result_id"] = "bad"
        elif mismatch == "place":
            args["place_id"] = "kakao:other"
        elif mismatch == "digest":
            stored.result.context_token_digest = "0" * 64
        else:
            repo.get_by_token_digest.return_value = None
        with pytest.raises(ServiceError) as error:
            await service.resolve(registered.result["context_token"], **args)
        assert error.value.code == "INVALID_CONTEXT"

    asyncio.run(run())


@pytest.mark.parametrize(
    "general,change,code",
    [
        (True, False, None),
        (True, True, "CONTEXT_CONDITIONS_MISMATCH"),
        (False, False, "INVALID_CONTEXT"),
    ],
)
def test_retry_compares_actual_conditions_and_requires_general_candidates(general, change, code):
    async def run():
        service, _, data, registered, stored = await issued(general=general)
        conditions = data.condition_update.conditions.model_copy(deep=True)
        if change:
            conditions.headcount += 1
        request = RetryRoutesRequest(
            request_id=uuid4(),
            trip_id=data.trip_id,
            condition_version=10,
            current_conditions=conditions,
            excluded_place_ids=["kakao:original"],
            result_id=data.result["result_id"],
            context_token=registered.result["context_token"],
        )
        if code:
            with pytest.raises(ServiceError) as error:
                await service.validate_retry(request)
            assert error.value.code == code
        else:
            assert await service.validate_retry(request) is stored
            # This port preserves original candidates; the processor applies exclusions.
            assert stored.cards[0].place_id == "kakao:original"

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    [
        "eta_value",
        "eta_source",
        "eta_time",
        "parking_value",
        "unknown_evidence",
        "population",
        "no_eta",
        "duplicate_evidence",
        "duplicate_place",
        "rank",
        "unknown_place",
        "unknown_route",
        "fake_parking",
        "nonfinite",
        "condition",
        "time_limit",
        "stages",
        "blank_reason",
    ],
)
def test_inconsistent_or_unverified_facts_are_rejected_before_storage(fault):
    async def run():
        data = response()
        card = data.result["cards"][0]
        evidence = card["reason"]["evidence"]
        if fault == "eta_value":
            evidence[0]["information"]["value"]["duration_seconds"] += 1
        elif fault == "eta_source":
            evidence[0]["information"]["source"] = "different"
        elif fault == "eta_time":
            evidence[0]["information"]["observed_at"] = NOW.isoformat()
        elif fault == "parking_value":
            evidence[1]["information"]["value"]["distance_meters"] += 1
        elif fault == "unknown_evidence":
            evidence[1]["information"]["status"] = "unavailable"
        elif fault == "population":
            evidence[1]["type"] = "population"
        elif fault == "no_eta":
            del evidence[0]
        elif fault == "duplicate_evidence":
            evidence.append(deepcopy(evidence[0]))
        elif fault == "duplicate_place":
            second = deepcopy(card)
            second["rank"] = 2
            data.result["cards"].append(second)
        elif fault == "rank":
            card["rank"] = 2
        elif fault == "unknown_place":
            card["place"]["status"] = "unavailable"
        elif fault == "unknown_route":
            card["route"]["status"] = "error"
        elif fault == "fake_parking":
            card["parking"]["status"] = "error"
        elif fault == "nonfinite":
            card["parking"]["nearest_distance_meters"] = float("nan")
        elif fault == "condition":
            data.result["conditions"]["headcount"] = 3
        elif fault == "time_limit":
            data.result["conditions"]["max_travel_seconds"] = 1
            data.condition_update.conditions.max_travel_seconds = 1
        elif fault == "stages":
            data.stages.routes = "failed"
        else:
            card["reason"]["text"] = " "
        repo = repository()
        with pytest.raises(ServiceError) as error:
            await RecommendationContextService(repo, clock=lambda: NOW).register(uuid4(), data)
        assert error.value.code == "INTERNAL_ERROR"
        repo.get.assert_not_awaited()
        repo.save.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["rank", "eta", "evidence"])
def test_general_candidates_cannot_claim_success(fault):
    async def run():
        data = response(general=True)
        card = data.result["cards"][0]
        successful = response().result["cards"][0]
        if fault == "rank":
            card["rank"] = 1
        elif fault == "eta":
            card["route"] = successful["route"]
        else:
            card["reason"]["evidence"] = successful["reason"]["evidence"]
        repo = repository()
        with pytest.raises(ServiceError) as error:
            await RecommendationContextService(repo, clock=lambda: NOW).register(uuid4(), data)
        assert error.value.code == "INTERNAL_ERROR"
        repo.save.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("freshness", ["future", "expired"])
def test_registration_cannot_restart_expiry_of_an_old_result(freshness):
    async def run():
        data, repo = response(), repository()
        data.result["created_at"] = (
            NOW + timedelta(seconds=1) if freshness == "future" else NOW - timedelta(hours=1)
        ).isoformat()
        with pytest.raises(ServiceError) as error:
            await RecommendationContextService(repo, clock=lambda: NOW).register(uuid4(), data)
        assert error.value.code == "INTERNAL_ERROR"
        repo.save.assert_not_awaited()

    asyncio.run(run())


def test_re_registering_issued_or_persisted_result_does_not_rotate_token():
    async def run():
        service, repo, original, registered, stored = await issued()
        digest = stored.result.context_token_digest
        for candidate in (registered, original):
            repo.get.return_value = stored
            with pytest.raises(ServiceError) as error:
                await service.register(uuid4(), candidate)
            assert error.value.code == "REQUEST_ID_CONFLICT"
        assert stored.result.context_token_digest == digest
        repo.save.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize(
    "error,code",
    [
        (ConditionVersionNotFound("private details"), "DATA_UNAVAILABLE"),
        (ConditionVersionMismatch("private details"), "CONTEXT_CONDITIONS_MISMATCH"),
        (SQLAlchemyError("private details"), "DATA_UNAVAILABLE"),
    ],
)
def test_repository_failures_are_safe_service_errors(error, code):
    async def run():
        repo = repository()
        repo.save.side_effect = error
        with pytest.raises(ServiceError) as raised:
            await RecommendationContextService(repo, clock=lambda: NOW).register(
                uuid4(), response()
            )
        assert raised.value.code == code and "private" not in str(raised.value)

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", [None, "missing", "kind", "conditions", "place"])
def test_retry_registration_binds_to_original_candidates(mismatch):
    async def run():
        service, repo, original, _, stored = await issued(general=True)
        data = response()
        data.trip_id = original.trip_id
        source_id = UUID(original.result["result_id"])
        if mismatch == "kind":
            stored.result.kind = "recommendation"
        elif mismatch == "conditions":
            stored.conditions.headcount += 1
        elif mismatch == "place":
            data.result["cards"][0]["place"]["value"]["place_id"] = "kakao:unrelated"
        repo.get.side_effect = [None, None if mismatch == "missing" else stored]
        if mismatch:
            with pytest.raises(ServiceError) as error:
                await service.register(uuid4(), data, source_result_id=source_id)
            assert error.value.code == (
                "CONTEXT_CONDITIONS_MISMATCH" if mismatch == "conditions" else "INVALID_CONTEXT"
            )
            assert repo.save.await_count == 1
        else:
            await service.register(uuid4(), data, source_result_id=source_id)
            assert repo.save.call_args.args[0].result.source_result_id == source_id

    asyncio.run(run())
