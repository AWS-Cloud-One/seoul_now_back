import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.exceptions import ServiceError
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.schemas.recommendations import Change, RecommendationRequest
from app.services.recommendation_adapters import RecommendationConditionsValidator
from app.services.recommendation_conditions import RecommendationConditionsService
from tests.fixtures.recommendation_context import response


@pytest.mark.parametrize(
    "invalid",
    [
        "unapplied",
        "wrong_version",
        "missing_change",
        "wrong_change",
        "duplicate_change",
        "missing_origin",
    ],
)
def test_unaccepted_or_inconsistent_decision_never_reaches_storage(invalid):
    async def run():
        request = RecommendationRequest(
            request_id=uuid4(),
            trip_id=uuid4(),
            condition_version=0,
            current_conditions=response().condition_update.conditions,
            conditions_patch={"headcount": 4},
            excluded_place_ids=[],
        )
        source = AsyncMock()
        source.region.return_value = "seoul"
        update = (await RecommendationConditionsValidator(source).validate(request)).update
        if invalid == "unapplied":
            update.applied = False
        elif invalid == "wrong_version":
            update.version = 0
        elif invalid == "missing_change":
            update.changes = []
        elif invalid == "wrong_change":
            update.changes[0].before = 99
        elif invalid == "duplicate_change":
            update.changes.append(update.changes[0])
        else:
            update.conditions.origin = None
            update.changes.append(
                Change(
                    field="origin",
                    before=request.current_conditions.origin.model_dump(),
                    after=None,
                )
            )
        repo = AsyncMock(spec=RecommendationConditionsRepository)
        with pytest.raises(ServiceError) as error:
            await RecommendationConditionsService(repo).accept(request, update)
        assert error.value.code == "INTERNAL_ERROR"
        repo.accept.assert_not_awaited()

    asyncio.run(run())
