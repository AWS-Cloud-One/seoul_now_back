"""R01 orchestration ports. No candidate search, condition merge or AI algorithm."""

from datetime import UTC, datetime
from typing import Protocol

from app.schemas.common import ApiError
from app.schemas.recommendations import (
    Clarification,
    Conditions,
    ConditionUpdate,
    RecommendationData,
    RecommendationEnvelope,
    RecommendationRequest,
    Stages,
)


class ValidationDecision:
    def __init__(self, update: ConditionUpdate, clarification: Clarification | None = None):
        self.update = update
        self.clarification = clarification


class ConditionsValidator(Protocol):
    async def validate(self, request: RecommendationRequest) -> ValidationDecision:
        """Revalidate current conditions, merge patch and verify origin geography."""
        ...


class RecommendationEngine(Protocol):
    async def recommend(
        self,
        request: RecommendationRequest,
        conditions: Conditions,
    ) -> RecommendationData:
        """Return the agreed R01 result without reinterpreting natural language."""
        ...


class RecommendationFailure(Exception):
    """Adapter error containing only safe, user-facing fields."""

    def __init__(
        self, status: int, code: str, message: str, retry_after_seconds: int | None = None
    ):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retry_after_seconds = retry_after_seconds


class RecommendationService:
    def __init__(
        self,
        validator: ConditionsValidator | None = None,
        engine: RecommendationEngine | None = None,
    ):
        self.validator, self.engine = validator, engine

    async def execute(self, request: RecommendationRequest) -> tuple[int, RecommendationEnvelope]:
        update = ConditionUpdate(
            applied=False,
            conditions=request.current_conditions,
            version=request.condition_version,
            changes=[],
        )
        try:
            if self.validator is None or self.engine is None:
                raise RecommendationFailure(
                    503,
                    "SERVICE_UNAVAILABLE",
                    "조건 검증·추천 기능이 아직 연결되지 않았습니다.",
                )
            decision = await self.validator.validate(request)
            candidate_update = decision.update
            if candidate_update.applied:
                changed = candidate_update.conditions != request.current_conditions
                if candidate_update.version != request.condition_version + int(changed):
                    raise ValueError("Validator returned an inconsistent condition version")
            elif (
                candidate_update.conditions != request.current_conditions
                or candidate_update.version != request.condition_version
                or candidate_update.changes
            ):
                raise ValueError("Unapplied conditions must preserve the request context")
            update = decision.update
            if decision.clarification is not None:
                if update.applied or update.changes or update.version != request.condition_version:
                    raise ValueError("Clarification must not apply conditions")
                data = RecommendationData(
                    trip_id=request.trip_id,
                    base_condition_version=request.condition_version,
                    kind="clarification",
                    message=decision.clarification.question,
                    condition_update=update,
                    stages=Stages(),
                    result=None,
                    clarification=decision.clarification,
                    warnings=[],
                )
            else:
                if not update.applied:
                    raise ValueError("Recommendation requires accepted conditions")
                data = RecommendationData.model_validate(
                    await self.engine.recommend(request, update.conditions),
                )
                if (
                    data.trip_id != request.trip_id
                    or data.base_condition_version != request.condition_version
                    or data.condition_update != update
                    or data.kind not in {"recommendation", "general_candidates", "no_candidates"}
                ):
                    raise ValueError("Recommendation adapter returned inconsistent context")
            return 200, RecommendationEnvelope(
                request_id=request.request_id,
                server_time=datetime.now(UTC),
                data=data,
                error=None,
            )
        except RecommendationFailure as exc:
            failure = exc
        except TimeoutError:
            failure = RecommendationFailure(
                504, "PROVIDER_TIMEOUT", "추천 처리 시간이 초과되었습니다."
            )
        except Exception:
            # Never return provider exceptions, credentials or database details.
            failure = RecommendationFailure(
                500, "INTERNAL_ERROR", "추천 처리 중 오류가 발생했습니다."
            )
        data = RecommendationData(
            trip_id=request.trip_id,
            base_condition_version=request.condition_version,
            kind="failure",
            message=failure.message,
            condition_update=update,
            stages=Stages(),
            result=None,
            clarification=None,
            warnings=[],
        )
        return failure.status, RecommendationEnvelope(
            request_id=request.request_id,
            server_time=datetime.now(UTC),
            data=data,
            error=ApiError(
                code=failure.code,
                message=failure.message,
                retryable=failure.status in {429, 502, 503, 504},
                retry_after_seconds=failure.retry_after_seconds,
            ),
        )


def get_recommendation_service() -> RecommendationService:
    """Replace this dependency with the team's validator and recommendation adapters."""
    return RecommendationService()
