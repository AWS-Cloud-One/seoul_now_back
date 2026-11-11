"""Origin confirmation drafts shared through PostgreSQL.

Same contract as `OriginContextStore`. Reservations stay in this process until
publication (they belong to one in-flight request). Publication is transactional:
when conditions are committed, it runs inside that same database transaction, so a
draft is never marked used unless the conditions really committed (and vice versa).
"""

from copy import deepcopy
from datetime import datetime, timedelta
from secrets import token_urlsafe
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.models.runtime_state import OriginContextRecord
from app.schemas.recommendations import Conditions, ConditionsPatch
from app.services.origin_context import OriginDraft, expired, invalid, stale
from app.services.origin_search import OriginSearchResult

RESULT = TypeAdapter(OriginSearchResult)


def unavailable():
    return ServiceError("DATA_UNAVAILABLE", "출발지 확인 상태를 저장·조회할 수 없습니다.")


def encode(draft):
    return {
        "version": draft.version,
        "conditions": draft.conditions.model_dump(mode="json"),
        # Omitted means unchanged; explicit null means clear. Keep that distinction.
        "patch": draft.patch.model_dump(mode="json", exclude_unset=True),
        "result": RESULT.dump_python(draft.result, mode="json"),
        "expires_at": draft.expires_at.isoformat(),
        "created_by": str(draft.created_by),
        "selected_id": draft.selected_id,
        "waiting_fields": list(draft.waiting_fields),
        "question": draft.question,
    }


def decode(token, trip_id, data, used_by):
    return OriginDraft(
        token=token,
        trip_id=trip_id,
        version=data["version"],
        conditions=Conditions.model_validate(data["conditions"]),
        patch=ConditionsPatch.model_validate(data["patch"]),
        result=RESULT.validate_python(data["result"]),
        expires_at=datetime.fromisoformat(data["expires_at"]),
        created_by=UUID(data["created_by"]),
        selected_id=data["selected_id"],
        waiting_fields=tuple(data["waiting_fields"]),
        question=data["question"],
        used_by=used_by,
    )


class PostgresOriginContextStore:
    transactional = True

    def __init__(self, session_factory, *, ttl_seconds=300, capacity=1000):
        if ttl_seconds <= 0 or capacity <= 0:
            raise ValueError("출발지 확인 보관 한도는 양수여야 합니다.")
        self.session_factory = session_factory
        self.ttl = timedelta(seconds=ttl_seconds)
        self.capacity = capacity
        self.reserved = {}

    async def load(self, token):
        """(draft, is_latest) for a live token, or None. The DB clock decides expiry."""
        try:
            async with self.session_factory() as session, session.begin():
                row = (
                    await session.execute(
                        select(
                            OriginContextRecord.trip_id,
                            OriginContextRecord.draft,
                            OriginContextRecord.used_by,
                            OriginContextRecord.is_latest,
                        ).where(
                            OriginContextRecord.token == token,
                            OriginContextRecord.expires_at > func.now(),
                        )
                    )
                ).first()
        except SQLAlchemyError:
            raise unavailable() from None
        if row is None:
            return None
        try:
            return decode(token, row.trip_id, row.draft, row.used_by), row.is_latest
        except KeyError, TypeError, ValueError:
            # Written by an incompatible version: treat as expired, never guess.
            return None

    async def peek(self, token):
        """Stored draft regardless of state (tests and diagnostics)."""
        loaded = await self.load(token)
        return None if loaded is None else loaded[0]

    async def resolve(self, token, request):
        loaded = await self.load(token)
        if loaded is None:
            raise expired()
        value, latest = loaded
        if value.trip_id != request.trip_id:
            raise invalid()
        if value.used_by is not None and value.used_by != request.request_id:
            raise stale()
        if not latest and value.used_by != request.request_id:
            raise stale()
        return value

    async def check_current(self, value, request):
        loaded = await self.load(value.token)
        if loaded is None:
            raise expired()
        stored, latest = loaded
        if stored.trip_id != request.trip_id:
            raise invalid()
        if stored.used_by is not None and stored.used_by != request.request_id:
            raise stale()
        if value.used_by is not None or not latest:
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
        try:
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    delete(OriginContextRecord).where(OriginContextRecord.expires_at <= func.now())
                )
                live = await session.scalar(select(func.count()).select_from(OriginContextRecord))
                now = await session.scalar(select(func.now()))
        except SQLAlchemyError:
            raise unavailable() from None
        if live + len(self.reserved) >= self.capacity:
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
        """Apply the transition; inside `session` when the caller owns the transaction."""
        before, after = transition.before, transition.after
        applied = outcome.response.condition_update.applied
        if outcome.failure is not None and not applied:
            return
        if after is None and not applied:
            return
        if session is None:
            async with self.session_factory() as own, own.begin():
                await self.write(own, before, after, request)
        else:
            await self.write(session, before, after, request)
        if after is not None:
            self.reserved.pop(after.token, None)

    @staticmethod
    async def write(session, before, after, request):
        if before is not None:
            await session.execute(
                update(OriginContextRecord)
                .where(OriginContextRecord.token == before.token)
                .values(used_by=request.request_id)
            )
        if after is not None:
            await session.execute(
                update(OriginContextRecord)
                .where(
                    OriginContextRecord.trip_id == request.trip_id,
                    OriginContextRecord.is_latest.is_(True),
                )
                .values(is_latest=False)
            )
            session.add(
                OriginContextRecord(
                    token=after.token,
                    trip_id=after.trip_id,
                    draft=encode(after),
                    used_by=None,
                    is_latest=True,
                    expires_at=after.expires_at,
                )
            )
            await session.flush()
        elif before is not None:
            await session.execute(
                update(OriginContextRecord)
                .where(
                    OriginContextRecord.token == before.token,
                    OriginContextRecord.trip_id == request.trip_id,
                )
                .values(is_latest=False)
            )

    async def discard(self, transition):
        if transition.after is not None:
            self.reserved.pop(transition.after.token, None)
