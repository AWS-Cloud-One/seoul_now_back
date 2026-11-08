"""Bounded P03 request replay, separate from 60-second route freshness and search state."""

import asyncio
from dataclasses import dataclass
from time import monotonic

from app.core.exceptions import ServiceError


@dataclass
class Entry:
    signature: str
    until: float
    future: asyncio.Future


class ReadReplay:
    def __init__(self, *, capacity=2000, ttl=1800, clock=monotonic):
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.entries = {}

    async def run(self, key, signature, compute):
        self.entries = {
            k: e for k, e in self.entries.items() if e.until > self.clock() or not e.future.done()
        }
        entry = self.entries.get(key)
        if entry is not None:
            if entry.signature != signature:
                raise ServiceError(
                    "REQUEST_ID_CONFLICT", "같은 요청 ID의 내용이 다릅니다.", retryable=False
                )
            value, error = await asyncio.shield(entry.future)
            if error:
                raise error
            return value.model_copy(deep=True)
        if len(self.entries) >= self.capacity:
            raise ServiceError("SERVICE_UNAVAILABLE", "요청 보관 한도에 도달했습니다.")
        entry = Entry(
            signature, self.clock() + self.ttl, asyncio.get_running_loop().create_future()
        )
        self.entries[key] = entry
        value, error = None, None
        try:
            value = await compute()
            return value.model_copy(deep=True)
        except ServiceError as exc:
            error = exc
            raise
        except BaseException:
            error = ServiceError("PROVIDER_TIMEOUT", "이동시간 확인이 중단되었습니다.")
            raise
        finally:
            entry.future.set_result((value, error))
