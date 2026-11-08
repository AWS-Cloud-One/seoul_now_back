"""Bounded single-process progress. No chat history or result-context ownership."""

import asyncio
import hashlib
import json
import time
from copy import copy
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
class TripProgress:
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conditions: object = None
    version: int | None = None
    excluded: set = field(default_factory=set)
    search: SearchProgress = field(default_factory=SearchProgress)
    requests: dict = field(default_factory=dict)
    expired: bool = False


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

    async def run(self, request, operation, callback, extra=None):
        now = self.clock()
        # Keep small tombstones: an expired ID must not silently lose exclusions.
        for entry in self.trips.values():
            if not entry.lock.locked() and now >= entry.expires_at and not entry.expired:
                entry.expired = True
                entry.requests.clear()
                entry.excluded.clear()
                entry.search = SearchProgress()
                entry.conditions = None
        key = str(request.trip_id)
        if key not in self.trips:
            if request.condition_version != 0:
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
        body["conditions_patch"] = request.conditions_patch.model_dump(
            mode="json", exclude_unset=True
        )
        signature = fingerprint({"operation": operation, "request": body, "extra": extra})
        async with entry.lock:
            if entry.expired or self.clock() >= entry.expires_at:
                raise ServiceError(
                    "CONTEXT_EXPIRED",
                    "추천 탐색 상태가 만료되었습니다. 새 여행을 시작해 주세요.",
                    retryable=False,
                )
            request_id = str(request.request_id)
            cached = entry.requests.get(request_id)
            if cached:
                if cached[0] != signature:
                    raise ServiceError(
                        "REQUEST_ID_CONFLICT", "같은 요청 ID의 내용이 다릅니다.", retryable=False
                    )
                return clone(cached[1])
            if len(entry.requests) >= self.max_requests:
                raise ServiceError(
                    "CONTEXT_EXPIRED",
                    "여행별 요청 한도에 도달했습니다. 새 여행을 시작해 주세요.",
                    retryable=False,
                )
            if entry.version is not None and (
                entry.version != request.condition_version
                or entry.conditions != request.current_conditions
            ):
                raise ServiceError(
                    "CONTEXT_CONDITIONS_MISMATCH",
                    "현재 여행의 조건과 버전을 다시 확인해 주세요.",
                    retryable=False,
                )
            outcome = await callback()
            update = outcome.response.condition_update
            if update.applied:
                entry.conditions = update.conditions.model_copy(deep=True)
                entry.version = update.version
                entry.excluded.update(request.excluded_place_ids)
                if outcome.response.kind == "recommendation":
                    entry.excluded.update(
                        c["place"]["value"]["place_id"] for c in outcome.response.result["cards"]
                    )
            entry.requests[request_id] = signature, clone(outcome)
            entry.expires_at = self.clock() + self.ttl_seconds
            return clone(outcome)


DEFAULT_PROGRESS_STORE = MemoryProgressStore()
