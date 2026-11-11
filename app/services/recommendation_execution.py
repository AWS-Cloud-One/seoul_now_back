"""One owner for replay, private search work, persistence and final publication.

The callback computes an outcome without acquiring another trip lock or saving
it. R01 and conversation adapters can share this boundary with their original
request, operation name and response/error factory.
"""

from dataclasses import replace

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.modules.recommendation.progress import DEFAULT_PROGRESS_STORE
from app.services.recommendation_persistence import CommitUncertain, RecommendationPersistence


class RecommendationExecution:
    def __init__(
        self,
        store=DEFAULT_PROGRESS_STORE,  # MemoryProgressStore or PostgresProgressStore
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
        publish=None,
        publish_transactional=False,
        replay_guard=None,
    ):
        """`publish(outcome, session=None)` applies pre-reserved state changes.

        A non-transactional publisher (in-process memory) runs only after the commit
        succeeded and must not fail. A transactional publisher (shared database) runs
        inside the condition commit, so both happen or neither does.
        """
        request = request.model_copy(deep=True)

        async def perform(work):
            try:
                if require_current:
                    await self.check_current(request, work)
                outcome = await callback(work)
                applied = outcome.response.condition_update.applied
                if applied:
                    self.store.check_current(request, work)
                published = False
                if self.persistence is not None and applied:
                    hooks = []
                    if work.commit_hook is not None:
                        # Shared trip state: lease check + state save in this transaction.
                        hooks.append(work.commit_hook)
                    if publish is not None and publish_transactional:
                        hooks.append(lambda session, final: publish(final, session))
                        published = True

                    async def before_commit(session, finalized):
                        final = replace(outcome, response=finalized)
                        for hook in hooks:
                            await hook(session, final)

                    finalized = await self.persistence.commit(
                        request,
                        outcome.response,
                        work,
                        source_result_id=source_result_id,
                        before_commit=before_commit if hooks else None,
                    )
                    outcome = replace(outcome, response=finalized)
                if publish is not None and not published:
                    await publish(outcome)
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
                request,
                operation,
                perform,
                extra,
                require_current=require_current,
                guard=guard,
                replay_guard=replay_guard,
            )
        except ServiceError as exc:
            return rejected(request, exc)
        except SQLAlchemyError:
            return rejected(
                request, ServiceError("DATA_UNAVAILABLE", "추천 상태를 저장·조회할 수 없습니다.")
            )
