"""Bounded opaque query cursors; no recommendation or result-context state."""

from copy import deepcopy
from secrets import token_hex
from time import monotonic

from app.core.exceptions import ServiceError


class QueryCursorStore:
    def __init__(self, *, ttl_seconds=300, capacity=2000, clock=monotonic):
        if ttl_seconds <= 0 or capacity <= 0:
            raise ValueError("Cursor limits must be positive")
        self.ttl, self.capacity, self.clock = ttl_seconds, capacity, clock
        self.entries = {}

    def issue(self, scope, payload):
        now = self.clock()
        self.entries = {key: entry for key, entry in self.entries.items() if entry[0] > now}
        if len(self.entries) >= self.capacity:
            raise ServiceError("SERVICE_UNAVAILABLE", "조회 커서 보관 한도에 도달했습니다.")
        token = token_hex(24)
        self.entries[token] = (now + self.ttl, deepcopy(scope), deepcopy(payload))
        return token

    def read(self, token, scope):
        if len(token) != 48 or any(c not in "0123456789abcdef" for c in token):
            raise ServiceError("INVALID_CURSOR", "조회 커서 형식이 올바르지 않습니다.")
        entry = self.entries.get(token)
        if entry is None or entry[0] <= self.clock():
            raise ServiceError(
                "CURSOR_EXPIRED", "조회 커서가 만료됐습니다. 첫 페이지부터 조회해 주세요."
            )
        if entry[1] != scope:
            raise ServiceError(
                "INVALID_CURSOR", "검색 조건·결과 개수를 이전 요청과 같게 입력해 주세요."
            )
        return deepcopy(entry[2])


QUERY_CURSORS = QueryCursorStore()
