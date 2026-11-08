"""One owner for replay, private search work, persistence and final publication.

The callback computes an outcome without acquiring another trip lock or saving
it. R01 and conversation adapters can share this boundary with their original
request, operation name and response/error factory.
"""

from dataclasses import replace

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.modules.recommendation.progress import DEFAULT_PROGRESS_STORE, MemoryProgressStore
from app.services.recommendation_persistence import CommitUncertain, RecommendationPersistence


class RecommendationExecution:
    def __init__(
        self,
        store: MemoryProgressStore = DEFAULT_PROGRESS_STORE,
        persistence: RecommendationPersistence | None = None,
    ):
        # Standalone adapters/tests may omit persistence; API dependencies always supply it.
        self.store, self.persistence = store, persistence

    async def check_current(self, request, work):
        self.store.check_current(request, work)
        if self.persistence is not None:
            await self.persistence.check_current(request, work)

    async def run(
        self,
        request,
        operation,
        callback,
        rejected,
        *,
        extra=None,
        source_result_id=None,
        require_current=True,
        guard=None,
    ):
        request = request.model_copy(deep=True)

        async def perform(work):
            try:
                if require_current:
                    await self.check_current(request, work)
                outcome = await callback(work)
                if outcome.response.condition_update.applied:
                    self.store.check_current(request, work)
                if self.persistence is not None and outcome.response.condition_update.applied:
                    finalized = await self.persistence.commit(
                        request, outcome.response, work, source_result_id=source_result_id
                    )
                    outcome = replace(outcome, response=finalized)
                return outcome
            except CommitUncertain as exc:
                work.invalidated = True
                return rejected(request, exc)
            except ServiceError as exc:
                return rejected(request, exc)
            except SQLAlchemyError:
                return rejected(
                    request,
                    ServiceError("DATA_UNAVAILABLE", "추천 상태를 저장·조회할 수 없습니다."),
                )
            except Exception:
                return rejected(
                    request, ServiceError("INTERNAL_ERROR", "추천 처리 중 오류가 발생했습니다.")
                )

        try:
            return await self.store.run(
                request, operation, perform, extra, require_current=require_current, guard=guard
            )
        except ServiceError as exc:
            return rejected(request, exc)
