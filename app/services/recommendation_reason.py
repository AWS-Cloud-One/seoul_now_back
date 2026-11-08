"""Answer an explicitly selected historical-reason intent using stored facts only.

The chat dispatcher selects this service after classifying the question. This
service does not interpret text, query providers, re-rank or register a new result.
"""

from pydantic import JsonValue, ValidationError

from app.core.exceptions import ServiceError
from app.models import RecommendationCard, RecommendationEvidence
from app.repositories.recommendation_context import RecommendationSnapshot
from app.schemas.common import Information
from app.schemas.pending import Answer, ChatData, ChatRequest, ChatStages
from app.schemas.recommendations import Conditions, ConditionUpdate
from app.services.recommendation_context import RecommendationContextService

PURPOSE_LABELS = {"cafe": "카페", "food": "음식점", "walk": "산책", "exhibition": "전시"}
PROVENANCE_FIELDS = ("freshness", "observed_at", "collected_at", "source", "reason_code")


def _unavailable() -> ServiceError:
    return ServiceError(
        "DATA_UNAVAILABLE", "저장된 당시 추천 근거를 확인할 수 없습니다.", retryable=False
    )


def _condition_text(conditions: Conditions) -> str:
    parts = []
    if conditions.origin is not None:
        parts.append(f"출발지 {conditions.origin.label}")
    if conditions.purposes:
        purposes = [
            PURPOSE_LABELS[item.purpose] + (f"(검색어: {item.keyword})" if item.keyword else "")
            for item in conditions.purposes
        ]
        parts.append("목적 " + ", ".join(purposes))
    if conditions.headcount is not None:
        parts.append(f"인원 {conditions.headcount}명")
    if conditions.max_travel_seconds is not None:
        parts.append(f"최대 자동차 이동시간 {conditions.max_travel_seconds}초")
    return "당시 요청 조건: " + ", ".join(parts) + ". " if parts else ""


def _information(card: RecommendationCard, item: RecommendationEvidence) -> Information[JsonValue]:
    """Expose original provenance, including unknown times; never substitute the answer time."""
    if item.type == "eta":
        if (
            card.route_status != "available"
            or item.duration_seconds is None
            or item.distance_meters is None
            or item.duration_seconds != card.duration_seconds
            or item.distance_meters != card.distance_meters
            or any(
                getattr(item, field) != getattr(card, f"route_{field}")
                for field in PROVENANCE_FIELDS
            )
        ):
            raise _unavailable()
        value = {
            "duration_seconds": item.duration_seconds,
            "distance_meters": float(item.distance_meters),
        }
    elif item.type == "parking":
        if (
            card.parking_status != "available"
            or item.duration_seconds is not None
            or item.distance_meters is None
            or item.distance_meters != card.parking_nearest_distance_meters
        ):
            raise _unavailable()
        value = {"distance_meters": float(item.distance_meters)}
    else:
        raise _unavailable()
    try:
        return Information[JsonValue](
            value=value,
            status="available",
            **{field: getattr(item, field) for field in PROVENANCE_FIELDS},
        )
    except ValidationError:
        raise _unavailable() from None


class RecommendationReasonService:
    def __init__(self, contexts: RecommendationContextService):
        self.contexts = contexts

    async def answer(self, request: ChatRequest) -> ChatData:
        """Keep current request conditions while explaining the selected card's old conditions."""
        request = request.model_copy(deep=True)
        target = request.target
        if target is None:
            # Ambiguous-target clarification belongs to the chat dispatcher.
            raise ServiceError(
                "INVALID_CONTEXT", "추천 이유를 확인할 카드를 선택해 주세요.", retryable=False
            )
        stored = await self.contexts.resolve(
            target.context_token,
            trip_id=request.trip_id,
            result_id=target.result_id,
            place_id=target.place_id,
        )
        return self.answer_from_context(request, stored)

    def answer_from_context(self, request: ChatRequest, stored: RecommendationSnapshot) -> ChatData:
        """Internal chat handoff: reuse the loaded snapshot and recheck expiry before answering."""
        target = request.target
        if target is None:
            raise ServiceError(
                "INVALID_CONTEXT", "추천 이유를 확인할 카드를 선택해 주세요.", retryable=False
            )
        self.contexts.verify_loaded(
            stored,
            target.context_token,
            trip_id=request.trip_id,
            result_id=target.result_id,
            place_id=target.place_id,
        )
        card = next((item for item in stored.cards if item.place_id == target.place_id), None)
        if card is None:
            raise ServiceError(
                "INVALID_CONTEXT", "추천 결과의 장소를 확인할 수 없습니다.", retryable=False
            )
        information = []
        if stored.result.kind == "general_candidates":
            message = (
                f"당시 {card.place_name}은 자동차 이동시간과 순위를 확인하지 못한 일반 후보로 "
                "표시했습니다. 확정된 추천 이유는 없습니다."
            )
        elif stored.result.kind == "recommendation":
            evidence = sorted(
                (item for item in stored.evidence if item.place_id == target.place_id),
                key=lambda item: item.position,
            )
            kinds = [item.type for item in evidence]
            if (
                not card.reason_text
                or not card.reason_text.strip()
                or card.reason_source not in {"ai", "fallback"}
                or "eta" not in kinds
                or len(kinds) != len(set(kinds))
            ):
                raise _unavailable()
            information = [_information(card, item) for item in evidence]
            message = (
                _condition_text(stored.conditions)
                + f"{card.place_name}에 저장된 당시 추천 이유: {card.reason_text}"
            )
        else:
            raise _unavailable()
        return ChatData(
            trip_id=request.trip_id,
            base_condition_version=request.condition_version,
            kind="answer",
            message=message,
            condition_update=ConditionUpdate(
                applied=False,
                conditions=request.current_conditions.model_copy(deep=True),
                version=request.condition_version,
                changes=[],
            ),
            stages=ChatStages(),
            result=None,
            clarification=None,
            warnings=[],
            answer=Answer(
                topic="recommendation_reason",
                place_id=card.place_id,
                text=message,
                original_result_id=str(stored.result.result_id),
                original_recommended_at=stored.result.created_at,
                checked_at=None,
                information=information,
            ),
        )
