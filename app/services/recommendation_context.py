"""Issue and verify historical context before any external provider or model call.

register() participates in the caller's write transaction; commit before returning
its response to the client. Part 2 must cache that finalized response for request
replay. A digest cannot recover the token, and this service never rotates it on retry.
"""

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.exceptions import ServiceError
from app.models import RecommendationResult
from app.repositories.recommendation_context import (
    ConditionVersionMismatch,
    ConditionVersionNotFound,
    RecommendationContextRepository,
    RecommendationSnapshot,
)
from app.schemas.pending import RetryRoutesRequest
from app.schemas.recommendations import RecommendationResponse
from app.services.recommendation_context_payload import ContextResult, prepare_snapshot


def _now() -> datetime:
    return datetime.now(UTC)


def _invalid_context() -> ServiceError:
    return ServiceError("INVALID_CONTEXT", "추천 결과 문맥을 확인할 수 없습니다.", retryable=False)


def _conditions_mismatch() -> ServiceError:
    return ServiceError(
        "CONTEXT_CONDITIONS_MISMATCH",
        "당시 조건과 다릅니다. 새 추천을 요청해 주세요.",
        retryable=False,
    )


def _conflict() -> ServiceError:
    return ServiceError(
        "REQUEST_ID_CONFLICT",
        "이미 등록한 결과입니다. 보관한 응답을 사용해 주세요.",
        retryable=False,
    )


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class RecommendationContextService:
    def __init__(
        self,
        repository: RecommendationContextRepository,
        *,
        ttl_seconds: int = 1800,
        clock: Callable[[], datetime] = _now,
    ):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError("Context TTL must be between 1 and 86400 seconds")
        self.repository, self.ttl, self.clock = repository, timedelta(seconds=ttl_seconds), clock

    async def register(
        self,
        request_id: UUID,
        response: RecommendationResponse,
        *,
        source_result_id: UUID | None = None,
    ) -> RecommendationResponse:
        """Store a new server result and return a copy with its token; never own request replay."""
        try:
            payload = ContextResult.model_validate(response.result)
            if payload.context_token is not None or payload.context_expires_at is not None:
                raise _conflict()
            now = self.clock()
            expires_at = payload.created_at + self.ttl
            if payload.created_at > now or expires_at <= now:
                raise ValueError("A new context requires a current result time")
            token = secrets.token_urlsafe(32)
            snapshot = prepare_snapshot(
                response,
                request_id,
                token_digest=_digest(token),
                expires_at=expires_at,
                source_result_id=source_result_id,
            )
        except ValueError, TypeError:
            raise ServiceError(
                "INTERNAL_ERROR",
                "추천 결과와 근거가 일치하지 않아 저장할 수 없습니다.",
                retryable=False,
            ) from None
        try:
            if await self.repository.get(response.trip_id, payload.result_id) is not None:
                raise _conflict()
            if source_result_id is not None:
                source = await self.repository.get(response.trip_id, source_result_id)
                if source is None or source.result.kind != "general_candidates":
                    raise _invalid_context()
                if source.conditions != snapshot.conditions:
                    raise _conditions_mismatch()
                original_ids = {card.place_id for card in source.cards}
                if not {card.place_id for card in snapshot.cards} <= original_ids:
                    raise _invalid_context()
            await self.repository.save(snapshot)
        except ConditionVersionMismatch:
            raise _conditions_mismatch() from None
        except ConditionVersionNotFound:
            raise ServiceError(
                "DATA_UNAVAILABLE", "추천 조건 버전이 아직 저장되지 않았습니다.", retryable=False
            ) from None
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint == RecommendationResult.__table__.primary_key.name:
                raise _conflict() from None
            raise ServiceError("DATA_UNAVAILABLE", "추천 결과를 저장할 수 없습니다.") from None
        except SQLAlchemyError:
            raise ServiceError("DATA_UNAVAILABLE", "추천 결과를 저장할 수 없습니다.") from None
        registered = response.model_copy(deep=True)
        registered.result["context_token"] = token
        registered.result["context_expires_at"] = expires_at.isoformat()
        return registered

    async def resolve(
        self,
        context_token: str,
        *,
        trip_id: UUID | str | None = None,
        result_id: UUID | str | None = None,
        place_id: str | None = None,
    ) -> RecommendationSnapshot:
        """P01 can resolve by token alone; other callers also bind their supplied identifiers."""
        digest, trip, result = self._identity(context_token, trip_id, result_id)
        try:
            stored = await self.repository.get_by_token_digest(digest)
        except SQLAlchemyError, ConditionVersionNotFound:
            raise ServiceError("DATA_UNAVAILABLE", "추천 결과를 조회할 수 없습니다.") from None
        return self._verify(stored, digest, trip, result, place_id)

    def verify_loaded(
        self,
        stored: RecommendationSnapshot,
        context_token: str,
        *,
        trip_id: UUID | str | None = None,
        result_id: UUID | str | None = None,
        place_id: str | None = None,
    ) -> RecommendationSnapshot:
        """Recheck an internally loaded snapshot after interpretation, without a second DB read."""
        digest, trip, result = self._identity(context_token, trip_id, result_id)
        return self._verify(stored, digest, trip, result, place_id)

    @staticmethod
    def _identity(context_token, trip_id, result_id):
        if not isinstance(context_token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", context_token
        ):
            raise _invalid_context()
        try:
            trip = UUID(str(trip_id)) if trip_id is not None else None
            result = UUID(str(result_id)) if result_id is not None else None
        except ValueError, TypeError:
            raise _invalid_context() from None
        return _digest(context_token), trip, result

    def _verify(self, stored, digest, trip, result, place_id):
        if (
            stored is None
            or not hmac.compare_digest(digest, stored.result.context_token_digest)
            or (trip is not None and stored.result.trip_id != trip)
            or (result is not None and stored.result.result_id != result)
            or (place_id is not None and place_id not in {card.place_id for card in stored.cards})
        ):
            raise _invalid_context()
        if self.clock() >= stored.result.context_expires_at:
            raise ServiceError(
                "CONTEXT_EXPIRED", "추천 결과 문맥이 만료되었습니다.", retryable=False
            )
        return stored

    async def validate_retry(self, request: RetryRoutesRequest) -> RecommendationSnapshot:
        """Return the original candidates; the part 2 processor applies exclusions and reroutes."""
        stored = await self.resolve(
            request.context_token,
            trip_id=request.trip_id,
            result_id=request.result_id,
        )
        if stored.result.kind != "general_candidates":
            raise _invalid_context()
        # Reverting an edit can produce equal conditions at a newer numeric version.
        if stored.conditions != request.current_conditions:
            raise _conditions_mismatch()
        return stored
