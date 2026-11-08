"""R01 orchestration ports. No candidate search, condition merge or AI algorithm."""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.schemas.recommendations import (
    Clarification,
    Conditions,
    ConditionUpdate,
    RecommendationRequest,
    RecommendationResponse,
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
    ) -> RecommendationResponse:
        """Return the agreed R01 result without reinterpreting natural language."""
        ...


class RecommendationFailure(ServiceError):
    """Adapter failure with a contract code and safe message, without HTTP state."""


@dataclass(frozen=True)
class RecommendationOutcome:
    response: RecommendationResponse
    failure: ServiceError | None = None


class RecommendationService:
    def __init__(
        self,
        validator: ConditionsValidator | None = None,
        engine: RecommendationEngine | None = None,
    ):
        self.validator, self.engine = validator, engine

    async def execute(self, request: RecommendationRequest) -> RecommendationOutcome:
        return await self._dispatch(request)

    async def retry_routes(
        self, request: RecommendationRequest, candidates
    ) -> RecommendationOutcome:
        """Part 3 must authenticate the original result/conditions before calling this port."""
        if request.conditions_patch.model_fields_set:
            return self._rejected(
                request,
                RecommendationFailure(
                    "CONTEXT_CONDITIONS_MISMATCH", "경로 재시도는 원래 조건을 사용해야 합니다."
                ),
            )
        return await self._dispatch(request, candidates)

    async def _dispatch(self, request, candidates=None):
        store = vars(self.engine).get("progress_store") if self.engine is not None else None
        try:
            if store is None:
                return await self._execute(request, candidates)
            return await store.run(
                request,
                "recommend" if candidates is None else "retry_routes",
                lambda: self._execute(request, candidates),
                None if candidates is None else [c.model_dump(mode="json") for c in candidates],
            )
        except ServiceError as exc:
            return self._rejected(request, exc)

    def _rejected(self, request, failure):
        return RecommendationOutcome(
            RecommendationResponse(
                trip_id=request.trip_id,
                base_condition_version=request.condition_version,
                kind="failure",
                message=failure.message,
                condition_update=ConditionUpdate(
                    applied=False,
                    conditions=request.current_conditions,
                    version=request.condition_version,
                    changes=[],
                ),
                stages=Stages(),
                result=None,
                clarification=None,
                warnings=[],
            ),
            failure,
        )

    async def _execute(self, request, candidates=None):
        update = ConditionUpdate(
            applied=False,
            conditions=request.current_conditions,
            version=request.condition_version,
            changes=[],
        )
        try:
            if self.validator is None or self.engine is None:
                raise RecommendationFailure(
                    "SERVICE_UNAVAILABLE",
                    "조건 검증·추천 기능이 아직 연결되지 않았습니다.",
                )
            decision = await self.validator.validate(request.model_copy(deep=True))
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
                data = RecommendationResponse(
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
                run = self.engine.recommend if candidates is None else self.engine.retry_routes
                args = () if candidates is None else (candidates,)
                data = RecommendationResponse.model_validate(
                    await run(
                        request.model_copy(deep=True),
                        update.conditions.model_copy(deep=True),
                        *args,
                    ),
                )
                if (
                    data.trip_id != request.trip_id
                    or data.base_condition_version != request.condition_version
                    or data.condition_update != update
                    or data.kind not in {"recommendation", "general_candidates", "no_candidates"}
                ):
                    raise ValueError("Recommendation adapter returned inconsistent context")
            return RecommendationOutcome(data)
        except ServiceError as exc:
            failure = exc
        except SQLAlchemyError:
            failure = RecommendationFailure("DATA_UNAVAILABLE", "추천 자료를 조회할 수 없습니다.")
        except TimeoutError:
            failure = RecommendationFailure("PROVIDER_TIMEOUT", "추천 처리 시간이 초과되었습니다.")
        except Exception:
            # Never return provider exceptions, credentials or database details.
            failure = RecommendationFailure("INTERNAL_ERROR", "추천 처리 중 오류가 발생했습니다.")
        data = RecommendationResponse(
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
        return RecommendationOutcome(data, failure)
