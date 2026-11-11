"""Trip search progress shared through PostgreSQL.

Same contract as `MemoryProgressStore`, so a trip continues on any API process and
across a rolling restart. Differences that matter:

* **Lease instead of an in-process lock.** One request per trip runs at a time across
  all processes. The lease is a row column (`lock_owner`/`lock_until`), not a held
  connection: callbacks call AI and external APIs for many seconds and the pool is
  small. A heartbeat extends the lease while the callback runs; if a process dies the
  lease simply expires. Saving requires still owning the lease (fencing).
* **Database clock.** Expiry and leases use `now()` of the database, never a process clock.
* **Request cache in a table**, so a state save never rewrites every cached answer.
* **Nothing is dropped silently.** Unknown state shapes raise; an incompatible stored
  schema expires that trip (the user starts a new trip, as after a TTL expiry).
"""

import asyncio
import random
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.models.runtime_state import TripProgressRecord, TripRequestRecord
from app.modules.recommendation.progress import (
    TripProgress,
    check_current,
    fingerprint,
    merge_outcome,
    working_copy,
)
from app.services.progress_state import (
    StateSchemaError,
    decode_outcome,
    decode_trip,
    encode_outcome,
    encode_trip,
)

TOMBSTONE_RETENTION = timedelta(days=1)


def unavailable():
    return ServiceError("DATA_UNAVAILABLE", "추천 상태를 저장·조회할 수 없습니다.")


def busy():
    return ServiceError(
        "SERVICE_UNAVAILABLE",
        "같은 여행의 이전 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요.",
        retryable=True,
        retry_after_seconds=1,
    )


def lost_lease():
    # The callback may already have committed; never guess that the trip is safe to resume.
    return ServiceError(
        "DATA_UNAVAILABLE",
        "추천 상태 저장을 확인할 수 없습니다. 새 여행을 시작해 주세요.",
        retryable=False,
    )


class Held:
    """A trip whose lease this request owns, with the state loaded under that lease."""

    def __init__(self, trip_id, owner, entry, *, created):
        self.trip_id, self.owner, self.entry, self.created = trip_id, owner, entry, created
        self.dirty = False  # state must be saved even if the request fails
        self.released = False
        self.heartbeat = None
        # Trip state written inside the condition commit; durable only if that commit
        # succeeded, which execute() confirms from the database.
        self.in_commit = None


class PostgresProgressStore:
    check_current = staticmethod(check_current)

    def __init__(
        self,
        session_factory,
        *,
        ttl_seconds=1800,
        max_trips=1000,
        max_requests=200,
        # A heartbeat renews the lease every third of it while a request runs, so the
        # lease only bounds how long a crashed process can block its trips.
        lease_seconds=30,
        wait_seconds=60,
    ):
        self.session_factory = session_factory
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_trips, self.max_requests = max_trips, max_requests
        self.lease = timedelta(seconds=lease_seconds)
        self.wait_seconds = wait_seconds
        self.local = {}

    # ---- inspection helpers (tests and diagnostics) -------------------------------------

    async def peek(self, trip_id):
        """Decoded published state of a trip, or None. Does not take the lease."""
        async with self.session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(TripProgressRecord.state, TripProgressRecord.expired).where(
                        TripProgressRecord.trip_id == UUID(str(trip_id))
                    )
                )
            ).first()
        if row is None or row.state is None:
            return None
        return decode_trip(row.state, expired=row.expired)

    async def request_count(self, trip_id):
        async with self.session_factory() as session, session.begin():
            return await session.scalar(
                select(func.count())
                .select_from(TripRequestRecord)
                .where(TripRequestRecord.trip_id == UUID(str(trip_id)))
            )

    # ---- lease -----------------------------------------------------------------------------

    @asynccontextmanager
    async def serialized(self, key):
        """Queue same-process requests locally so only one polls the database per trip."""
        slot = self.local.setdefault(key, [asyncio.Lock(), 0])
        slot[1] += 1
        try:
            async with slot[0]:
                yield
        finally:
            slot[1] -= 1
            if slot[1] == 0:
                self.local.pop(key, None)

    async def try_acquire(self, trip_id, owner):
        """Return (created, state, expired, expires_at, now) when the lease is ours, else None."""
        lease_until = func.now() + self.lease
        async with self.session_factory() as session, session.begin():
            created = (
                await session.execute(
                    insert(TripProgressRecord)
                    .values(
                        trip_id=trip_id,
                        state=None,
                        expired=False,
                        expires_at=func.now() + self.ttl,
                        lock_owner=owner,
                        lock_until=lease_until,
                    )
                    .on_conflict_do_nothing()
                    .returning(TripProgressRecord.trip_id, func.now().label("db_now"))
                )
            ).first()
            if created is not None:
                return True, None, False, None, created.db_now
            row = (
                await session.execute(
                    update(TripProgressRecord)
                    .where(
                        TripProgressRecord.trip_id == trip_id,
                        (TripProgressRecord.lock_owner.is_(None))
                        | (TripProgressRecord.lock_until < func.now()),
                    )
                    .values(lock_owner=owner, lock_until=lease_until)
                    .returning(
                        TripProgressRecord.state,
                        TripProgressRecord.expired,
                        TripProgressRecord.expires_at,
                        func.now().label("db_now"),
                    )
                )
            ).first()
        if row is None:
            return None
        return False, row.state, row.expired, row.expires_at, row.db_now

    async def acquire(self, trip_id):
        owner = uuid4()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.wait_seconds
        while True:
            try:
                got = await self.try_acquire(trip_id, owner)
            except SQLAlchemyError:
                raise unavailable() from None
            if got is not None:
                break
            if loop.time() >= deadline:
                raise busy()
            await asyncio.sleep(0.05 + random.random() * 0.1)
        created, state, expired, expires_at, now = got
        if created:
            return Held(trip_id, owner, TripProgress(expires_at=0.0), created=True)
        if state is None:
            entry = TripProgress(expires_at=0.0)
            held = Held(trip_id, owner, entry, created=False)
        else:
            try:
                entry = decode_trip(state, expired=expired)
                held = Held(trip_id, owner, entry, created=False)
            except StateSchemaError, KeyError, TypeError, ValueError:
                entry = TripProgress(expires_at=0.0)
                entry.expire()
                held = Held(trip_id, owner, entry, created=False)
                held.dirty = True
        if now >= expires_at and not entry.expired:
            # Keep a small tombstone: an expired ID must not silently lose exclusions.
            entry.expire()
            held.dirty = True
        return held

    async def renew(self, held):
        interval = self.lease.total_seconds() / 3
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.session_factory() as session, session.begin():
                    result = await session.execute(
                        update(TripProgressRecord)
                        .where(
                            TripProgressRecord.trip_id == held.trip_id,
                            TripProgressRecord.lock_owner == held.owner,
                        )
                        .values(lock_until=func.now() + self.lease)
                    )
            except SQLAlchemyError:
                continue  # the next beat retries; the lease only matters when it expires
            if result.rowcount == 0:
                return

    # ---- saving ------------------------------------------------------------------------------

    async def save(self, held, *, new_request=None, extend=False):
        """Persist state (and the new cached answer) and release the lease, atomically."""
        entry = held.entry
        values = {
            "state": encode_trip(entry),
            "expired": entry.expired,
            "lock_owner": None,
            "lock_until": None,
            "updated_at": func.now(),
        }
        if extend:
            values["expires_at"] = func.now() + self.ttl
        try:
            async with self.session_factory() as session, session.begin():
                saved = await session.execute(
                    update(TripProgressRecord)
                    .where(
                        TripProgressRecord.trip_id == held.trip_id,
                        TripProgressRecord.lock_owner == held.owner,
                    )
                    .values(**values)
                    .returning(TripProgressRecord.trip_id)
                )
                if saved.first() is None:
                    raise lost_lease()
                if entry.expired:
                    await session.execute(
                        delete(TripRequestRecord).where(TripRequestRecord.trip_id == held.trip_id)
                    )
                if new_request is not None:
                    request_id, signature, outcome = new_request
                    await session.execute(
                        insert(TripRequestRecord)
                        .values(
                            trip_id=held.trip_id,
                            request_id=request_id,
                            signature=signature,
                            outcome=outcome,
                        )
                        .on_conflict_do_nothing()
                    )
        except SQLAlchemyError:
            raise unavailable() from None
        held.released = True

    async def persist_in_commit(
        self, session, held, request, operation, work, signature, request_id, outcome
    ):
        """Inside the condition commit: still own the lease, then save state and answer.

        `FOR UPDATE` holds the trip row until the commit, so no other process can take
        the lease in between. If the lease is gone the whole commit is rolled back,
        and the conditions never get ahead of the shared trip state.
        """
        owned = await session.execute(
            select(TripProgressRecord.trip_id)
            .where(
                TripProgressRecord.trip_id == held.trip_id,
                TripProgressRecord.lock_owner == held.owner,
            )
            .with_for_update()
        )
        if owned.first() is None:
            raise lost_lease()
        entry = deepcopy(held.entry)
        merge_outcome(entry, operation, request, work, outcome)
        await session.execute(
            update(TripProgressRecord)
            .where(
                TripProgressRecord.trip_id == held.trip_id,
                TripProgressRecord.lock_owner == held.owner,
            )
            .values(
                state=encode_trip(entry),
                expired=entry.expired,
                expires_at=func.now() + self.ttl,
                updated_at=func.now(),
            )
        )
        await session.execute(
            insert(TripRequestRecord)
            .values(
                trip_id=held.trip_id,
                request_id=request_id,
                signature=signature,
                outcome=encode_outcome(outcome),
            )
            .on_conflict_do_nothing()
        )
        held.in_commit = (entry, request_id, signature)

    async def saved_in_commit(self, held):
        """Did the condition commit that carried the trip state actually commit?

        The commit may still fail after persist_in_commit ran (deferred constraints,
        lost connection), rolling the trip state and cached answer back with it.
        """
        entry, request_id, signature = held.in_commit
        try:
            async with self.session_factory() as session, session.begin():
                stored = await session.scalar(
                    select(TripRequestRecord.signature).where(
                        TripRequestRecord.trip_id == held.trip_id,
                        TripRequestRecord.request_id == request_id,
                    )
                )
        except SQLAlchemyError:
            raise unavailable() from None
        return entry if stored == signature else None

    async def release(self, held):
        """Give the lease back without changing state (the request changed nothing)."""
        try:
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    update(TripProgressRecord)
                    .where(
                        TripProgressRecord.trip_id == held.trip_id,
                        TripProgressRecord.lock_owner == held.owner,
                    )
                    .values(lock_owner=None, lock_until=None)
                )
        except SQLAlchemyError:
            return  # the lease expires on its own
        held.released = True

    async def discard_new(self, held):
        """Remove a row this request just created and will not use."""
        try:
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    delete(TripProgressRecord).where(
                        TripProgressRecord.trip_id == held.trip_id,
                        TripProgressRecord.lock_owner == held.owner,
                        TripProgressRecord.state.is_(None),
                    )
                )
        except SQLAlchemyError:
            return
        held.released = True

    async def finish(self, held):
        if held.released:
            return
        if held.dirty:
            try:
                await self.save(held)
                return
            except ServiceError:
                pass  # cannot persist the tombstone; the next request fails its DB check
        await self.release(held)

    async def check_capacity(self, held):
        try:
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    delete(TripProgressRecord).where(
                        TripProgressRecord.expires_at < func.now() - TOMBSTONE_RETENTION,
                        TripProgressRecord.lock_owner.is_(None),
                    )
                )
                live = await session.scalar(
                    select(func.count())
                    .select_from(TripProgressRecord)
                    .where(TripProgressRecord.expires_at > func.now())
                )
        except SQLAlchemyError:
            raise unavailable() from None
        if live > self.max_trips:  # includes the row this request just created
            raise ServiceError("SERVICE_UNAVAILABLE", "추천 상태 저장 한도에 도달했습니다.")

    async def load_cached(self, held, request_id):
        async with self.session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(TripRequestRecord.signature, TripRequestRecord.outcome).where(
                        TripRequestRecord.trip_id == held.trip_id,
                        TripRequestRecord.request_id == request_id,
                    )
                )
            ).first()
            count = await session.scalar(
                select(func.count())
                .select_from(TripRequestRecord)
                .where(TripRequestRecord.trip_id == held.trip_id)
            )
        return row, count

    # ---- the contract shared with MemoryProgressStore -------------------------------

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
        body = request.model_dump(mode="json")
        # Default-valued null and explicitly supplied null have different patch semantics.
        if "conditions_patch" in type(request).model_fields:
            body["conditions_patch"] = request.conditions_patch.model_dump(
                mode="json", exclude_unset=True
            )
        signature = fingerprint({"operation": operation, "request": body, "extra": extra})
        trip_id = UUID(str(request.trip_id))
        async with self.serialized(trip_id):
            held = await self.acquire(trip_id)
            held.heartbeat = asyncio.create_task(self.renew(held))
            try:
                return await self.execute(
                    held,
                    request,
                    operation,
                    callback,
                    signature,
                    require_current,
                    guard,
                    replay_guard,
                )
            finally:
                held.heartbeat.cancel()
                await asyncio.shield(self.finish(held))

    async def execute(
        self, held, request, operation, callback, signature, require_current, guard, replay_guard
    ):
        entry = held.entry
        if held.created:
            if require_current and request.condition_version != 0:
                await self.discard_new(held)
                raise ServiceError(
                    "CONTEXT_EXPIRED",
                    "여행의 추천 상태가 없습니다. 새 여행을 시작해 주세요.",
                    retryable=False,
                )
            try:
                await self.check_capacity(held)
            except ServiceError:
                await self.discard_new(held)
                raise
        if require_current and entry.expired:
            raise ServiceError(
                "CONTEXT_EXPIRED",
                "추천 탐색 상태가 만료되었습니다. 새 여행을 시작해 주세요.",
                retryable=False,
            )
        request_id = UUID(str(request.request_id))
        cached, count = (None, 0)
        if not entry.expired:
            try:
                cached, count = await self.load_cached(held, request_id)
            except SQLAlchemyError:
                raise unavailable() from None
        if cached is not None and cached.signature != signature:
            raise ServiceError(
                "REQUEST_ID_CONFLICT", "같은 요청 ID의 내용이 다릅니다.", retryable=False
            )
        # Target authentication/expiry is checked even on a cached response,
        # after any lock wait. It must not invoke paid interpretation.
        if guard is not None:
            await guard()
        if cached is not None:
            try:
                outcome = decode_outcome(cached.outcome)
            except StateSchemaError, KeyError, ValueError:
                raise unavailable() from None
            if replay_guard is not None:
                # A cached question may itself carry a now-expired context,
                # even when the original request contained no context token.
                await replay_guard(outcome, working_copy(entry))
            return outcome
        if count >= self.max_requests:
            raise ServiceError(
                "CONTEXT_EXPIRED",
                "여행별 요청 한도에 도달했습니다. 새 여행을 시작해 주세요.",
                retryable=False,
            )
        work = working_copy(entry)
        if require_current:
            check_current(request, work)

        async def commit_hook(session, final):
            await self.persist_in_commit(
                session, held, request, operation, work, signature, request_id, final
            )

        # Applied conditions commit together with the trip state (see persist_in_commit).
        work.commit_hook = commit_hook
        try:
            # The execution owner returns only after any DB commit succeeds.
            outcome = await callback(work)
        except BaseException:
            # Cancellation/interrupt may arrive while commit is completing.
            # With no durable replay journal, never guess that the trip is safe
            # to resume. Keep a tombstone rather than replay external effects.
            entry.expire()
            held.dirty = True
            raise
        if work.invalidated:
            entry.expire()
            held.dirty = True  # finish() retries the tombstone if this save fails
            await self.save(held)
            return outcome
        if held.in_commit is not None:
            committed = await self.saved_in_commit(held)
            held.in_commit = None
            if committed is not None:
                # State and cached answer were saved inside the condition commit.
                # Only the lease remains; if releasing fails it simply expires.
                held.entry = committed
                await self.release(held)
                return outcome
            # That commit rolled back: nothing was applied. Cache the returned
            # (failure) outcome below so a retry replays it instead of redoing work.
        merge_outcome(entry, operation, request, work, outcome)
        await self.save(
            held,
            new_request=(request_id, signature, encode_outcome(outcome)),
            extend=True,
        )
        return outcome
