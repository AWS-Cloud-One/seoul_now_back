"""Bounded origin drafts. Publish transitions inside the trip lock, after DB commit."""

import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from uuid import UUID

from app.core.exceptions import ServiceError
from app.schemas.origin import OriginClarificationContext, OriginInterpretationContext, OriginReply
from app.schemas.recommendations import Clarification, Conditions, ConditionsPatch
from app.services.origin_search import OriginSearchResult

FIELD_LABELS = {
    "origin": "출발지",
    "purposes": "방문 목적",
    "headcount": "정확한 인원",
    "group_type": "모임 유형",
    "age_groups": "연령대",
    "max_travel_seconds": "이동시간 제한",
}


def expired():
    return ServiceError(
        "CONTEXT_EXPIRED",
        "출발지 확인 문맥이 만료됐습니다. 출발지를 다시 검색해 주세요.",
        retryable=False,
    )


def invalid():
    return ServiceError(
        "INVALID_CONTEXT", "출발지 선택 문맥을 확인할 수 없습니다.", retryable=False
    )


def stale():
    return ServiceError(
        "CONTEXT_CONDITIONS_MISMATCH",
        "최신 조건과 출발지 확인 질문을 사용해 주세요.",
        retryable=False,
    )


def missing_conditions(conditions):
    return (["origin"] if conditions.origin is None else []) + (
        ["purposes"] if not conditions.purposes else []
    )


@dataclass(frozen=True)
class OriginDraft:
    token: str = field(repr=False)
    trip_id: UUID
    version: int
    conditions: Conditions
    patch: ConditionsPatch
    result: OriginSearchResult
    expires_at: datetime
    created_by: UUID
    selected_id: str | None = None
    waiting_fields: tuple[str, ...] = ()
    question: str | None = None
    used_by: UUID | None = None

    def clarification(self):
        if self.selected_id is not None:
            return Clarification(
                question=self.question or "추가 조건을 확인해 주세요.",
                required_fields=list(self.waiting_fields),
                pending_patch=self.patch.model_copy(deep=True),
                choices=[],
                origin_context=OriginClarificationContext(
                    context_token=self.token,
                    mode="confirmed",
                    expires_at=self.expires_at,
                    search_limited=self.result.search_limited,
                ),
            )
        value = self.result.clarification(
            context_token=self.token, expires_at=self.expires_at, pending_patch=self.patch
        )
        if self.question and self.result.mode == "refine":
            value.question = self.question
        return value

    def interpretation_context(self):
        if self.selected_id is not None:
            return OriginInterpretationContext(
                mode="confirmed",
                candidates=[
                    {"name": self.patch.origin.label, "address": self.patch.origin.address}
                ],
            )
        return OriginInterpretationContext(
            mode=self.result.mode,
            candidates=[
                {"name": c.location.label, "address": c.location.address}
                for c in self.result.candidates
            ],
        )

    def candidate(self, identifier):
        if self.selected_id is not None:
            raise stale()
        value = next((c for c in self.result.candidates if c.candidate_id == identifier), None)
        if value is None or not value.location.supported:
            raise invalid()
        return value


@dataclass
class OriginTransition:
    before: OriginDraft | None = None
    after: OriginDraft | None = None


class OriginContextStore:
    """Single-process drafts. `PostgresOriginContextStore` shares the same async contract."""

    # Publication runs after the DB commit here; a shared store publishes inside it.
    transactional = False

    def __init__(self, *, ttl_seconds=300, capacity=1000, clock=lambda: datetime.now(UTC)):
        if ttl_seconds <= 0 or capacity <= 0:
            raise ValueError("출발지 확인 보관 한도는 양수여야 합니다.")
        self.ttl = timedelta(seconds=ttl_seconds)
        self.capacity, self.clock = capacity, clock
        self.entries, self.latest, self.reserved = {}, {}, {}

    @staticmethod
    def request_token(request):
        if request.origin_action is not None:
            return request.origin_action.context_token
        pending = request.pending_clarification
        return pending.origin_context.context_token if pending and pending.origin_context else None

    async def resolve(self, token, request):
        value = self.entries.get(token)
        if value is None or self.clock() >= value.expires_at:
            raise expired()
        if value.trip_id != request.trip_id:
            raise invalid()
        if value.used_by is not None and value.used_by != request.request_id:
            raise stale()
        if self.latest.get(value.trip_id) != token and value.used_by != request.request_id:
            raise stale()
        return deepcopy(value)

    async def check_current(self, value, request):
        await self.resolve(value.token, request)
        if value.used_by is not None or self.latest.get(value.trip_id) != value.token:
            raise stale()
        if (
            value.version != request.condition_version
            or value.conditions != request.current_conditions
        ):
            raise stale()

    async def reserve(
        self,
        request,
        result,
        patch,
        *,
        previous=None,
        new_search=False,
        selected_id=None,
        waiting_fields=(),
        question=None,
    ):
        now = self.clock()
        self.entries = {key: entry for key, entry in self.entries.items() if entry.expires_at > now}
        self.latest = {trip: token for trip, token in self.latest.items() if token in self.entries}
        if len(self.entries) + len(self.reserved) >= self.capacity:
            raise ServiceError("SERVICE_UNAVAILABLE", "출발지 확인 보관 한도에 도달했습니다.")
        value = OriginDraft(
            token=token_urlsafe(32),
            trip_id=request.trip_id,
            version=request.condition_version,
            conditions=request.current_conditions.model_copy(deep=True),
            patch=patch.model_copy(deep=True),
            result=deepcopy(result),
            expires_at=now + self.ttl if previous is None or new_search else previous.expires_at,
            created_by=request.request_id,
            selected_id=selected_id,
            waiting_fields=tuple(waiting_fields),
            question=question,
        )
        self.reserved[value.token] = value
        return value

    async def publish(self, transition, request, outcome, session=None):
        """No failure-prone work here: slots and response data were prepared before commit."""
        before, after = transition.before, transition.after
        applied = outcome.response.condition_update.applied
        if outcome.failure is not None and not applied:
            return
        if after is None and not applied:
            return
        if before is not None and before.token in self.entries:
            self.entries[before.token] = replace(
                self.entries[before.token], used_by=request.request_id
            )
        if after is not None:
            self.reserved.pop(after.token, None)
            self.entries[after.token] = after
            self.latest[request.trip_id] = after.token
        elif before is not None and self.latest.get(request.trip_id) == before.token:
            self.latest.pop(request.trip_id, None)

    async def discard(self, transition):
        if transition.after is not None:
            self.reserved.pop(transition.after.token, None)


def simple_origin_reply(draft, message):
    value = re.sub(r"[\s.!?。！？]", "", message)
    if value in {"다른후보", "다른후보보기"} or (
        draft.selected_id is None and value in {"아니", "아니요"}
    ):
        return OriginReply(action="show_alternatives")
    if value in {"다시입력", "출발지다시입력", "다시검색"}:
        return OriginReply(action="restart")
    if draft.selected_id is not None:
        return None
    selected = re.fullmatch(r"([0-9]{1,3})번(?:이요|요|으로|으로요)?", value)
    if selected:
        if not 1 <= int(selected[1]) <= len(draft.result.candidates):
            return "ambiguous"
        return OriginReply(action="select", candidate_index=int(selected[1]))
    if value in {"응", "네", "예", "맞아", "맞아요", "여기맞아", "여기서출발", "여기서출발할게"}:
        if draft.result.mode == "confirm":
            return OriginReply(action="confirm")
        return "ambiguous"
    return None
