"""Short authoritative DB checks and atomic condition/result commits.

Use independent read/write session factories, never the request's long-lived
read-only provider session. External I/O is complete before commit() acquires
any condition lock. No token or accepted update escapes before commit succeeds.
"""

from sqlalchemy.exc import DBAPIError

from app.core.exceptions import ServiceError
from app.modules.recommendation.progress import RecommendationWork
from app.repositories.recommendation_conditions import RecommendationConditionsRepository
from app.repositories.recommendation_context import RecommendationContextRepository
from app.schemas.recommendations import RecommendationResponse, RequestContext
from app.services.recommendation_conditions import RecommendationConditionsService
from app.services.recommendation_context import RecommendationContextService


class CommitUncertain(ServiceError):
    """A lost connection during commit cannot prove either success or rollback."""

    def __init__(self):
        super().__init__(
            "DATA_UNAVAILABLE",
            "저장 완료 여부를 확인할 수 없습니다. 새 여행을 시작해 주세요.",
            retryable=False,
        )


class RecommendationPersistence:
    def __init__(self, readers, writers, *, context_ttl_seconds=1800):
        self.readers, self.writers = readers, writers
        self.context_ttl_seconds = context_ttl_seconds

    async def check_current(self, request: RequestContext, work: RecommendationWork) -> None:
        async with self.readers() as session:
            current = await RecommendationConditionsRepository(session).get_current(request.trip_id)
        if (current is None) != (work.version is None) or (
            current is None and request.condition_version != 0
        ):
            # Even a saved version-0 trip must not become a new trip after memory loss.
            raise ServiceError(
                "CONTEXT_EXPIRED",
                "여행의 추천 상태를 복원할 수 없습니다. 새 여행을 시작해 주세요.",
                retryable=False,
            )
        if current is not None and (
            current.version != request.condition_version
            or current.conditions != request.current_conditions
            or current.version != work.version
            or current.conditions != work.conditions
        ):
            raise ServiceError(
                "CONTEXT_CONDITIONS_MISMATCH",
                "현재 여행의 조건과 버전을 다시 확인해 주세요.",
                retryable=False,
            )

    async def commit(
        self,
        request: RequestContext,
        response: RecommendationResponse,
        work: RecommendationWork,
        *,
        source_result_id=None,
    ) -> RecommendationResponse:
        ready_to_commit = False
        try:
            async with self.writers.begin() as session:
                await RecommendationConditionsService(
                    RecommendationConditionsRepository(session)
                ).accept(
                    request, response.condition_update, expected_existing=work.version is not None
                )
                finalized = response
                if response.result is not None:
                    finalized = await RecommendationContextService(
                        RecommendationContextRepository(session),
                        ttl_seconds=self.context_ttl_seconds,
                    ).register(request.request_id, response, source_result_id=source_result_id)
                ready_to_commit = True
        except DBAPIError as exc:
            if ready_to_commit and exc.connection_invalidated:
                raise CommitUncertain() from None
            raise
        # Exiting begin() is the commit boundary, not merely returning from register().
        return finalized
