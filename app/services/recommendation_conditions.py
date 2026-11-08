"""Persist an already validated condition decision in the caller's transaction.

Validation/provider calls finish before this short write phase. A successful
return is provisional until the caller commits conditions and any result;
publishing memory state and caching the final response belong to that caller.
"""

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.repositories.recommendation_conditions import (
    ConditionSessionChanged,
    ConditionValuesInvalid,
    ConditionVersionMismatch,
    ConditionVersionNotFound,
    RecommendationConditionsRepository,
)
from app.schemas.recommendations import ConditionUpdate, RequestContext


class RecommendationConditionsService:
    def __init__(self, repository: RecommendationConditionsRepository):
        self.repository = repository

    async def accept(
        self,
        request: RequestContext,
        update: ConditionUpdate,
        *,
        expected_existing: bool | None = None,
    ) -> ConditionUpdate:
        request, update = request.model_copy(deep=True), update.model_copy(deep=True)
        before = request.current_conditions.model_dump(mode="json")
        after = update.conditions.model_dump(mode="json")
        expected = {
            field: {"field": field, "before": before[field], "after": value}
            for field, value in after.items()
            if before[field] != value
        }
        changes = {change.field: change.model_dump(mode="json") for change in update.changes}
        if (
            not update.applied
            or update.version != request.condition_version + bool(expected)
            or len(changes) != len(update.changes)
            or changes != expected
            or update.conditions.origin is None
            or not update.conditions.purposes
            or not update.conditions.origin.supported
            or update.conditions.origin.region not in {"seoul", "incheon", "gyeonggi"}
        ):
            raise ServiceError(
                "INTERNAL_ERROR",
                "검증된 조건 변경과 저장할 버전이 일치하지 않습니다.",
                retryable=False,
            )
        try:
            await self.repository.accept(
                request.trip_id,
                request.condition_version,
                request.current_conditions,
                update.conditions,
                expected_existing=expected_existing,
            )
        except ConditionVersionNotFound, ConditionSessionChanged:
            raise ServiceError(
                "CONTEXT_EXPIRED",
                "여행의 저장 상태를 확인할 수 없습니다. 새 여행을 시작해 주세요.",
                retryable=False,
            ) from None
        except ConditionVersionMismatch:
            raise ServiceError(
                "CONTEXT_CONDITIONS_MISMATCH",
                "현재 여행의 조건과 버전을 다시 확인해 주세요.",
                retryable=False,
            ) from None
        except ConditionValuesInvalid:
            raise ServiceError(
                "VALIDATION_ERROR",
                "목적·연령대의 중복과 미지정 값을 확인해 주세요.",
                retryable=False,
            ) from None
        except SQLAlchemyError:
            raise ServiceError("DATA_UNAVAILABLE", "추천 조건을 저장할 수 없습니다.") from None
        return update
