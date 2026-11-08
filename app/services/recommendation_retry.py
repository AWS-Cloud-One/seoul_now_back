"""R02: authenticate a general result and reroute only its saved candidates."""

from uuid import UUID

from app.core.exceptions import ServiceError
from app.modules.recommendation.types import Candidate
from app.schemas.pending import Place, RetryRoutesRequest
from app.schemas.recommendations import RecommendationRequest
from app.services.pending import PendingFailure, PendingService


class RecommendationRouteRetryProcessor:
    def __init__(self, recommendations, *, contexts, execution):
        self.recommendations, self.contexts, self.execution = recommendations, contexts, execution

    async def retry(self, request: RetryRoutesRequest):
        request = request.model_copy(deep=True)
        try:
            source_result_id = UUID(request.result_id)
        except ValueError:
            raise ServiceError(
                "INVALID_CONTEXT", "추천 결과 문맥을 확인할 수 없습니다.", retryable=False
            ) from None
        stored = None

        async def guard():
            nonlocal stored
            # Also runs before returning a cached response; tokens never bypass expiry.
            stored = await self.contexts.validate_retry(request)

        async def compute(work):
            candidates = [
                Candidate(
                    place=Place(
                        place_id=card.place_id,
                        name=card.place_name,
                        purpose=card.purpose,
                        category_code=card.category_code,
                        category_name=card.category_name,
                        coordinates=coordinates,
                        address=card.address,
                        road_address=card.road_address,
                        phone=card.phone,
                        provider_url=card.provider_url,
                    ),
                    collected_at=card.place_collected_at,
                )
                for card, coordinates in zip(stored.cards, stored.card_coordinates, strict=True)
            ]
            structured = RecommendationRequest.model_validate(
                request.model_dump(exclude={"result_id", "context_token"})
                | {"conditions_patch": {}}
            )
            outcome = await self.recommendations.prepare(structured, work, candidates)
            # A slow provider must not turn an expired source into a newly stored result.
            self.contexts.verify_loaded(
                stored,
                request.context_token,
                trip_id=request.trip_id,
                result_id=request.result_id,
            )
            PendingService.validate_context(outcome.response, request, retry=True)
            return outcome

        outcome = await self.execution.run(
            request,
            "retry_routes",
            compute,
            self.recommendations.rejected,
            source_result_id=source_result_id,
            guard=guard,
        )
        if outcome.failure is not None:
            exc = outcome.failure
            raise PendingFailure(
                exc.code,
                exc.message,
                data=outcome.response,
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after_seconds,
            )
        return outcome.response
