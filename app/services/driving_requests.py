"""Process-scoped route reuse and conservative provider admission, with no retries."""

import asyncio
import math
from collections import OrderedDict, deque
from datetime import UTC, datetime
from time import monotonic

from app.core.exceptions import ServiceError

ETA_TTL_SECONDS = 60


class DrivingRequests:
    def __init__(
        self,
        *,
        budget=8000,
        concurrency=3,
        interval=1 / 3,
        capacity=2000,
        clock=monotonic,
        wall_clock=lambda: datetime.now(UTC),
    ):
        self.budget, self.interval, self.capacity = budget, interval, capacity
        self.clock, self.wall_clock = clock, wall_clock
        self.entries = OrderedDict()
        self.pending = {}
        self.calls = deque()
        self.slots, self.admission = asyncio.Semaphore(concurrency), asyncio.Lock()
        self.next_start = self.blocked_until = 0.0

    def _prune(self):
        now = self.clock()
        for key in [key for key, (until, _) in self.entries.items() if until <= now]:
            del self.entries[key]
        while self.calls and self.calls[0] <= now - 86400:
            self.calls.popleft()

    def remember(self, key, value):
        """Also accepts authenticated stored ETA, without extending its receipt time."""
        at = value.route.collected_at
        if (
            at is None
            or value.route.status != "available"
            or value.route.source != "kakao_mobility"
        ):
            return
        age = (self.wall_clock() - at).total_seconds()
        if not 0 <= age < ETA_TTL_SECONDS:
            return
        self._prune()
        old = self.entries.get(key)
        if old is not None and old[1].route.collected_at >= at:
            return
        self.entries[key] = (self.clock() + ETA_TTL_SECONDS - age, value.model_copy(deep=True))
        self.entries.move_to_end(key)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)

    def _copy(self, value):
        result = value.model_copy(deep=True)
        result.checked_at = self.wall_clock()
        return result

    def _limited(self, until):
        return ServiceError(
            "RATE_LIMITED",
            "길찾기 호출을 잠시 쉬고 있습니다. 잠시 후 다시 확인해 주세요.",
            retry_after_seconds=max(1, math.ceil(until - self.clock())),
        )

    async def _admit(self):
        async with self.admission:
            self._prune()
            if self.blocked_until > self.clock():
                raise self._limited(self.blocked_until)
            if len(self.calls) >= self.budget:
                raise self._limited(self.calls[0] + 86400)
            wait = self.next_start - self.clock()
            if wait > 0:
                await asyncio.sleep(wait)
            # Another in-flight request may have received a quota error while we waited.
            if self.blocked_until > self.clock():
                raise self._limited(self.blocked_until)
            self.calls.append(self.clock())  # Count attempts, including failed requests.
            self.next_start = self.clock() + self.interval

    async def get(self, key, fetch, *, aliases=()):
        self._prune()
        keys = (key, *aliases)
        cached = [(self.entries[k][0], k) for k in keys if k in self.entries]
        if cached:
            _, found = max(cached, key=lambda entry: entry[0])
            self.entries.move_to_end(found)
            return self._copy(self.entries[found][1])
        future = next((self.pending[k] for k in keys if k in self.pending), None)
        if future is not None:
            value, error = await asyncio.shield(future)
            if error:
                raise error
            return self._copy(value)
        if len(self.pending) >= 64:
            raise self._limited(self.clock() + 1)
        future = self.pending[key] = asyncio.get_running_loop().create_future()
        value, error = None, None
        try:
            async with self.slots:
                await self._admit()
                try:
                    value = await fetch()
                except ServiceError as exc:
                    if exc.code == "RATE_LIMITED":
                        wait = exc.retry_after_seconds or 60
                        self.blocked_until = max(self.blocked_until, self.clock() + wait)
                    raise
            self.remember(key, value)
            return self._copy(value)
        except ServiceError as exc:
            error = exc
            raise
        except BaseException:
            error = ServiceError("PROVIDER_TIMEOUT", "길찾기 조회가 중단되었습니다.")
            raise
        finally:
            # The owner owns its request's HTTP client. No detached background requests.
            self.pending.pop(key, None)
            future.set_result((value, error))
