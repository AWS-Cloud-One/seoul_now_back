"""Team integration ports for provisional APIs; no fabricated success responses.

Adapters own domain validation and safe ServiceError mapping. Context adapters
must verify tokens, result membership, expiry and original conditions BEFORE
fetching data or invoking paid services. No token/auth policy is invented here.
"""

from dataclasses import dataclass
from typing import Protocol

from app.core.exceptions import ServiceError
from app.schemas.pending import (
    ChatData,
    ChatRequest,
    ChatStages,
    DataStatusData,
    PlaceDetailData,
    RetryRoutesRequest,
    TravelTimeData,
    TravelTimeRequest,
)
from app.schemas.recommendations import ConditionUpdate, RecommendationResponse


def unavailable(message):
    return ServiceError("SERVICE_UNAVAILABLE", message, retryable=False)


class PendingFailure(ServiceError):
    """Safe adapter failure explicitly carrying the last accepted condition state."""

    def __init__(self, code, message, *, data, retryable=None, retry_after_seconds=None):
        super().__init__(
            code, message, retryable=retryable, retry_after_seconds=retry_after_seconds
        )
        self.data = data


@dataclass(frozen=True)
class ConversationOutcome:
    response: ChatData | RecommendationResponse
    failure: ServiceError | None = None
    # Private replay bindings; never serialized into the public response.
    targets: tuple = ()


class ChatProcessor(Protocol):
    async def process(self, request: ChatRequest) -> ChatData:
        """Interpret/revalidate, answer or recommend once; verify target context.

        Do not call the R01 HTTP endpoint a second time. Accepted-condition
        failures must raise PendingFailure with ChatData(kind='failure').
        """
        ...


class RouteRetryProcessor(Protocol):
    async def retry(self, request: RetryRoutesRequest) -> RecommendationResponse:
        """Verify historical context; retry original candidates, never search new ones."""
        ...


class PlaceReader(Protocol):
    async def detail(self, place_id: str, context_token: str) -> PlaceDetailData:
        """Verify token/place membership; retain original coordinates and card facts."""
        ...


class TravelTimeReader(Protocol):
    async def check(self, place_id: str, request: TravelTimeRequest) -> TravelTimeData:
        """Verify context, reuse fresh data, then fetch only if necessary; no ranking."""
        ...


class DataStatusReader(Protocol):
    async def read(self, source: str, target_key: str) -> DataStatusData:
        """Validate source/target against DB; return only sanitized summary fields."""
        ...


class PendingService:
    def __init__(
        self,
        *,
        chat: ChatProcessor | None = None,
        retry: RouteRetryProcessor | None = None,
        places: PlaceReader | None = None,
        travel: TravelTimeReader | None = None,
        status: DataStatusReader | None = None,
    ):
        self.chat, self.retry, self.places = chat, retry, places
        self.travel, self.status = travel, status

    @staticmethod
    def validate_context(data, request, retry):
        change = data.condition_update
        if (
            data.trip_id != request.trip_id
            or data.base_condition_version != request.condition_version
        ):
            raise ValueError("Adapter returned inconsistent request context")
        if not change.applied and (
            change.conditions != request.current_conditions
            or change.version != request.condition_version
            or change.changes
        ):
            raise ValueError("Unapplied conditions must remain unchanged")
        if change.applied and change.version != request.condition_version + int(
            change.conditions != request.current_conditions
        ):
            raise ValueError("Inconsistent condition version")
        before = request.current_conditions.model_dump(mode="json")
        after = change.conditions.model_dump(mode="json")
        expected_changes = {
            field: (before[field], value)
            for field, value in after.items()
            if value != before[field]
        }
        actual_changes = {item.field: (item.before, item.after) for item in change.changes}
        if len(actual_changes) != len(change.changes) or actual_changes != expected_changes:
            raise ValueError("Change details must match the actual condition changes")
        if retry and (
            change.conditions != request.current_conditions
            or change.changes
            or change.version != request.condition_version
            or data.kind not in {"recommendation", "general_candidates", "no_candidates", "failure"}
        ):
            raise ValueError("Route retry must not change conditions or ask new questions")

    async def conversation(
        self, request: ChatRequest | RetryRoutesRequest, *, retry=False
    ) -> ConversationOutcome:
        """Preserve incoming conditions when the request has not been accepted."""
        data_type = RecommendationResponse if retry else ChatData
        update = ConditionUpdate(
            applied=False,
            conditions=request.current_conditions,
            version=request.condition_version,
            changes=[],
        )
        try:
            adapter = self.retry if retry else self.chat
            if adapter is None:
                raise unavailable(
                    "경로 재확인 기능이 아직 연결되지 않았습니다."
                    if retry
                    else "채팅 해석·검증·추천 기능이 아직 연결되지 않았습니다."
                )
            # Adapters may mutate their working input; preserve the incoming context
            # for response validation and failures that did not accept new conditions.
            adapter_request = request.model_copy(deep=True)
            raw = await (
                adapter.retry(adapter_request) if retry else adapter.process(adapter_request)
            )
            data = data_type.model_validate(raw)
            self.validate_context(data, request, retry)
            # Failure after acceptance must explicitly preserve the accepted update.
            if data.kind == "failure":
                raise ValueError("Use a response carrying a safe failure status and error")
            return ConversationOutcome(data)
        except PendingFailure as exc:
            failure = exc
            try:
                failed_data = data_type.model_validate(exc.data)
                self.validate_context(failed_data, request, retry)
                if failed_data.kind != "failure":
                    raise ValueError("Invalid adapter failure")
                return ConversationOutcome(failed_data, failure)
            except Exception:
                failure = ServiceError("INTERNAL_ERROR", "요청 처리 중 오류가 발생했습니다.")
        except ServiceError as exc:
            failure = exc
        except TimeoutError:
            failure = ServiceError("PROVIDER_TIMEOUT", "처리 시간이 초과되었습니다.")
        except Exception:
            failure = ServiceError("INTERNAL_ERROR", "요청 처리 중 오류가 발생했습니다.")
        data = data_type(
            trip_id=request.trip_id,
            base_condition_version=request.condition_version,
            kind="failure",
            message=failure.message,
            condition_update=update,
            stages=ChatStages() if not retry else {},
            result=None,
            clarification=None,
            warnings=[],
        )
        return ConversationOutcome(data, failure)

    async def place_detail(self, place_id, context_token):
        if self.places is None:
            raise unavailable("결과 문맥 검증·장소 상세 기능이 아직 연결되지 않았습니다.")
        data = PlaceDetailData.model_validate(await self.places.detail(place_id, context_token))
        if data.place.value is not None and data.place.value.place_id != place_id:
            raise ValueError("Place adapter returned another place")
        return data

    async def travel_time(self, place_id, request):
        if self.travel is None:
            raise unavailable("결과 문맥 검증·이동시간 확인 기능이 아직 연결되지 않았습니다.")
        data = TravelTimeData.model_validate(
            await self.travel.check(place_id, request.model_copy(deep=True))
        )
        if (
            data.trip_id != request.trip_id
            or data.result_id != request.result_id
            or data.place_id != place_id
            or data.base_condition_version != request.condition_version
        ):
            raise ValueError("Travel adapter returned inconsistent context")
        return data

    async def data_status(self, source, target_key):
        if self.status is None:
            raise unavailable("수집 상태 조회 기능이 아직 연결되지 않았습니다.")
        data = DataStatusData.model_validate(await self.status.read(source, target_key))
        if data.source != source or data.target_key != target_key:
            raise ValueError("Status adapter returned another target")
        return data
