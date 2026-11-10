"""Bounded single-process progress. No chat history or result-context ownership."""

import asyncio
import hashlib
import json
import time
from copy import copy, deepcopy
from dataclasses import dataclass, field

from app.core.exceptions import ServiceError


@dataclass
class SearchProgress:
    fingerprint: str = ""
    pages: dict = field(default_factory=dict)
    ended: set = field(default_factory=set)
    remaining: list = field(default_factory=list)
    seen: set = field(default_factory=set)
    areas: list | None = None
    area_warnings: list = field(default_factory=list)


@dataclass
class RecommendationWork:
    """Private request workspace; engines never mutate the published trip."""

    version: int | None
    conditions: object
    excluded: set
    search: SearchProgress
    invalidated: bool = False
    expired: bool = False
    condition_draft: object = None
    conversation_draft: object = None
    answered_targets: list = field(default_factory=list)


@dataclass
class TripProgress:
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conditions: object = None
    version: int | None = None
    excluded: set = field(default_factory=set)
    search: SearchProgress = field(default_factory=SearchProgress)
    requests: dict = field(default_factory=dict)
    expired: bool = False
    condition_draft: object = None
    conversation_draft: object = None

    def expire(self):
        self.expired = True
        self.requests.clear()
        self.excluded.clear()
        self.search = SearchProgress()
        self.conditions = None
        self.condition_draft = None
        self.conversation_draft = None


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def clone(outcome):
    cloned = copy(outcome)
    # Frozen dataclass: exceptions need explicit construction, not deepcopy.
    object.__setattr__(cloned, "response", outcome.response.model_copy(deep=True))
    if outcome.failure is not None:
        failure = outcome.failure
        object.__setattr__(
            cloned,
            "failure",
            ServiceError(
                failure.code,
                failure.message,
                retryable=failure.retryable,
                retry_after_seconds=failure.retry_after_seconds,
            ),
        )
    return cloned


class MemoryProgressStore:
    def __init__(self, ttl_seconds=1800, max_trips=1000, max_requests=200, clock=time.monotonic):
        self.ttl_seconds, self.max_trips, self.max_requests = ttl_seconds, max_trips, max_requests
        self.clock, self.trips = clock, {}

    def trip(self, trip_id):
        return self.trips[str(trip_id)]

    @staticmethod
    def check_current(request, work):
        if work.expired or (work.version is None and request.condition_version != 0):
            raise ServiceError(
                "CONTEXT_EXPIRED",
                "추천 탐색 상태가 없습니다. 새 여행을 시작해 주세요.",
                retryable=False,
            )
        if work.version is not None and (
            work.version != request.condition_version
            or work.conditions != request.current_conditions
        ):
            raise ServiceError(
                "CONTEXT_CONDITIONS_MISMATCH",
                "현재 여행의 조건과 버전을 다시 확인해 주세요.",
                retryable=False,
            )

    @staticmethod
    def working_copy(entry):
        return RecommendationWork(
            version=entry.version,
            conditions=deepcopy(entry.conditions),
            excluded=set(entry.excluded),
            search=deepcopy(entry.search),
            expired=entry.expired,
            condition_draft=deepcopy(entry.condition_draft),
            conversation_draft=deepcopy(entry.conversation_draft),
        )

    async def run(
        self,
        request,
        operation,
        callback,
        extra=None,
        *,
        require_current=True,
        guard=None,
        replay_guard=None,
    ):
        now = self.clock()
        # Keep small tombstones: an expired ID must not silently lose exclusions.
        for entry in self.trips.values():
            if not entry.lock.locked() and now >= entry.expires_at:
                entry.expire()
        key = str(request.trip_id)
        if key not in self.trips:
            if require_current and request.condition_version != 0:
                raise ServiceError(
                    "CONTEXT_EXPIRED",
                    "여행의 추천 상태가 없습니다. 새 여행을 시작해 주세요.",
                    retryable=False,
                )
            if len(self.trips) >= self.max_trips:
                raise ServiceError("SERVICE_UNAVAILABLE", "추천 상태 저장 한도에 도달했습니다.")
            self.trips[key] = TripProgress(now + self.ttl_seconds)
        entry = self.trips[key]
        body = request.model_dump(mode="json")
        # Default-valued null and explicitly supplied null have different patch semantics.
        if "conditions_patch" in type(request).model_fields:
            body["conditions_patch"] = request.conditions_patch.model_dump(
                mode="json", exclude_unset=True
            )
        signature = fingerprint({"operation": operation, "request": body, "extra": extra})
        async with entry.lock:
            if self.clock() >= entry.expires_at:
                entry.expire()
            if require_current and entry.expired:
                raise ServiceError(
                    "CONTEXT_EXPIRED",
                    "추천 탐색 상태가 만료되었습니다. 새 여행을 시작해 주세요.",
                    retryable=False,
                )
            request_id = str(request.request_id)
            cached = entry.requests.get(request_id)
            if cached and cached[0] != signature:
                raise ServiceError(
                    "REQUEST_ID_CONFLICT", "같은 요청 ID의 내용이 다릅니다.", retryable=False
                )
            # Target authentication/expiry is checked even on a cached response,
            # after any lock wait. It must not invoke paid interpretation.
            if guard is not None:
                await guard()
            if cached:
                outcome = clone(cached[1])
                if replay_guard is not None:
                    # A cached question may itself carry a now-expired context,
                    # even when the original request contained no context token.
                    await replay_guard(outcome, self.working_copy(entry))
                return outcome
            if len(entry.requests) >= self.max_requests:
                raise ServiceError(
                    "CONTEXT_EXPIRED",
                    "여행별 요청 한도에 도달했습니다. 새 여행을 시작해 주세요.",
                    retryable=False,
                )
            work = self.working_copy(entry)
            if require_current:
                self.check_current(request, work)
            try:
                # The execution owner returns only after any DB commit succeeds.
                outcome = await callback(work)
            except BaseException:
                # Cancellation/interrupt may arrive while commit is completing.
                # With no durable replay journal, never guess that the trip is safe
                # to resume. Keep a tombstone rather than replay external effects.
                entry.expire()
                raise
            if work.invalidated:
                entry.expire()
                return clone(outcome)
            cached_outcome = clone(outcome)
            update = outcome.response.condition_update
            if update.applied:
                entry.condition_draft = None
                entry.conversation_draft = (
                    deepcopy(work.conversation_draft) if operation == "chat" else None
                )
                entry.conditions = update.conditions.model_copy(deep=True)
                entry.version = update.version
                entry.excluded.update(request.excluded_place_ids)
                if outcome.failure is None:
                    entry.search = work.search
                if outcome.response.kind == "recommendation":
                    entry.excluded.update(
                        c["place"]["value"]["place_id"] for c in outcome.response.result["cards"]
                    )
            elif operation == "chat" and outcome.failure is None:
                entry.condition_draft = deepcopy(work.condition_draft)
                entry.conversation_draft = deepcopy(work.conversation_draft)
            entry.requests[request_id] = signature, cached_outcome
            entry.expires_at = self.clock() + self.ttl_seconds
            return clone(outcome)


DEFAULT_PROGRESS_STORE = MemoryProgressStore()
