"""C01 adapter: interpret once, then call the same structured recommendation service."""

from app.clients.kakao import KakaoError
from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import interpret
from app.schemas.location import Coordinates, Location
from app.schemas.pending import Answer, ChatData, ChatStages
from app.schemas.recommendations import (
    Clarification,
    Conditions,
    ConditionsPatch,
    ConditionUpdate,
    RecommendationRequest,
)
from app.services.locations import documents, provider_failure, text
from app.services.pending import PendingFailure


class RecommendationChatProcessor:
    def __init__(self, model, kakao, recommendations):
        self.model, self.kakao, self.recommendations = model, kakao, recommendations

    @staticmethod
    def response(request, kind, message, **values):
        return ChatData(
            trip_id=request.trip_id,
            base_condition_version=request.condition_version,
            kind=kind,
            message=message,
            condition_update=ConditionUpdate(
                applied=False,
                conditions=request.current_conditions,
                version=request.condition_version,
                changes=[],
            ),
            stages=ChatStages(interpretation="ok"),
            result=None,
            clarification=values.pop("clarification", None),
            warnings=[],
            **values,
        )

    async def process(self, request):
        if request.target is not None:
            raise ServiceError(
                "SERVICE_UNAVAILABLE",
                "과거 추천 문맥 검증·답변 기능이 아직 연결되지 않았습니다.",
                retryable=False,
            )
        try:
            parsed = await interpret(self.model, request)
            extracted_patch = parsed.patch()
        except ServiceError as exc:
            data = self.response(request, "failure", exc.message)
            data.stages.interpretation = "failed"
            raise PendingFailure(
                exc.code,
                exc.message,
                data=data,
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after_seconds,
            ) from None
        if parsed.intent == "scope_notice":
            message = (
                "현재 출발하는 자동차 이동 기준 장소 추천을 지원합니다. "
                "요청한 정보는 보장할 수 없습니다."
            )
            return self.response(
                request,
                "answer",
                message,
                answer=Answer(
                    topic="scope_notice",
                    place_id=None,
                    text=message,
                    original_result_id=None,
                    original_recommended_at=None,
                    checked_at=None,
                    information=[],
                ),
            )
        patch = (
            request.pending_clarification.pending_patch.model_dump(exclude_unset=True)
            if (request.pending_clarification)
            else {}
        )
        patch.update(extracted_patch.model_dump(exclude_unset=True))
        unresolved_origin = False
        if parsed.origin_name:
            try:
                rows, _ = documents(await self.kakao.keyword(parsed.origin_name, page=1, size=5))
                exact = [row for row in rows if text(row.get("place_name")) == parsed.origin_name]
                if len(exact) == 1:
                    row = exact[0]
                    patch["origin"] = Location(
                        label=parsed.origin_name,
                        coordinates=Coordinates(latitude=row["y"], longitude=row["x"]),
                        address=text(row.get("address_name")),
                        region=None,
                        supported=False,
                    )
                else:
                    unresolved_origin = True
            except KakaoError as exc:
                raise provider_failure(exc) from None
            except ValueError, TypeError, KeyError:
                raise ServiceError(
                    "PROVIDER_ERROR", "출발지 검색 결과를 검증할 수 없습니다."
                ) from None
        patch = ConditionsPatch.model_validate(patch)
        merged = request.current_conditions.model_dump()
        merged.update(patch.model_dump(exclude_unset=True))
        conditions = Conditions.model_validate(merged)
        required = []
        if unresolved_origin or conditions.origin is None:
            required.append("origin")
        if not conditions.purposes:
            required.append("purposes")
        if required or parsed.intent == "clarification":
            message = parsed.question or "출발지와 추천 목적을 확인해 주세요."
            return self.response(
                request,
                "clarification",
                message,
                clarification=Clarification(
                    question=message, required_fields=required, pending_patch=patch, choices=[]
                ),
            )
        structured = RecommendationRequest(
            **request.model_dump(
                include={
                    "request_id",
                    "trip_id",
                    "condition_version",
                    "current_conditions",
                    "excluded_place_ids",
                }
            ),
            conditions_patch=patch,
        )
        outcome = await self.recommendations.execute(structured)
        values = outcome.response.model_dump()
        values["stages"]["interpretation"] = "ok"
        data = ChatData.model_validate(values)
        if outcome.failure:
            exc = outcome.failure
            raise PendingFailure(
                exc.code,
                exc.message,
                data=data,
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after_seconds,
            )
        return data
