"""Bounded opaque query cursors; no recommendation or result-context state.

`QueryCursorStore` keeps cursors in one process (tests, local development).
`PostgresQueryCursorStore` shares them through PostgreSQL so every API process
and a rolling restart can resume the same cursor. Both expose the same async API.
"""

from copy import deepcopy
from datetime import timedelta
from secrets import token_hex
from time import monotonic

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.models.runtime_state import QueryCursor
from app.services.state_codec import to_json


def check_format(token):
    if len(token) != 48 or any(c not in "0123456789abcdef" for c in token):
        raise ServiceError("INVALID_CURSOR", "조회 커서 형식이 올바르지 않습니다.")


def expired():
    return ServiceError("CURSOR_EXPIRED", "조회 커서가 만료됐습니다. 첫 페이지부터 조회해 주세요.")


def mismatched():
    return ServiceError("INVALID_CURSOR", "검색 조건·결과 개수를 이전 요청과 같게 입력해 주세요.")


def full():
    return ServiceError("SERVICE_UNAVAILABLE", "조회 커서 보관 한도에 도달했습니다.")


class QueryCursorStore:
    def __init__(self, *, ttl_seconds=300, capacity=2000, clock=monotonic):
        if ttl_seconds <= 0 or capacity <= 0:
            raise ValueError("Cursor limits must be positive")
        self.ttl, self.capacity, self.clock = ttl_seconds, capacity, clock
        self.entries = {}

    async def issue(self, scope, payload):
        now = self.clock()
        self.entries = {key: entry for key, entry in self.entries.items() if entry[0] > now}
        if len(self.entries) >= self.capacity:
            raise full()
        token = token_hex(24)
        self.entries[token] = (now + self.ttl, deepcopy(scope), deepcopy(payload))
        return token

    async def read(self, token, scope):
        check_format(token)
        entry = self.entries.get(token)
        if entry is None or entry[0] <= self.clock():
            raise expired()
        if entry[1] != scope:
            raise mismatched()
        return deepcopy(entry[2])


QUERY_CURSORS = QueryCursorStore()


class PostgresQueryCursorStore:
    """Cursors shared through PostgreSQL. Each call uses its own short transaction.

    The database clock decides expiry, so API processes never disagree about it.
    Capacity is a soft limit: concurrent issues may briefly exceed it by a few rows.
    """

    def __init__(self, session_factory, *, ttl_seconds=300, capacity=2000):
        if ttl_seconds <= 0 or capacity <= 0:
            raise ValueError("Cursor limits must be positive")
        self.session_factory, self.ttl, self.capacity = session_factory, ttl_seconds, capacity

    async def issue(self, scope, payload):
        token = token_hex(24)
        scope, payload = to_json(scope), to_json(payload)
        try:
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    delete(QueryCursor).where(QueryCursor.expires_at <= func.now())
                )
                live = await session.scalar(select(func.count()).select_from(QueryCursor))
                if live >= self.capacity:
                    raise full()
                session.add(
                    QueryCursor(
                        token=token,
                        scope=scope,
                        payload=payload,
                        expires_at=func.now() + timedelta(seconds=self.ttl),
                    )
                )
        except SQLAlchemyError:
            raise ServiceError("DATA_UNAVAILABLE", "조회 커서를 저장할 수 없습니다.") from None
        return token

    async def read(self, token, scope):
        check_format(token)
        try:
            async with self.session_factory() as session, session.begin():
                row = (
                    await session.execute(
                        select(QueryCursor.scope, QueryCursor.payload).where(
                            QueryCursor.token == token, QueryCursor.expires_at > func.now()
                        )
                    )
                ).first()
        except SQLAlchemyError:
            raise ServiceError("DATA_UNAVAILABLE", "조회 커서를 확인할 수 없습니다.") from None
        if row is None:
            raise expired()
        if row.scope != to_json(scope):
            raise mismatched()
        return row.payload
