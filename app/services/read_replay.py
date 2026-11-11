"""Bounded P03 request replay, separate from 60-second route freshness and search state.

`ReadReplay` keeps results in one process (tests, local development).
`PostgresReadReplay` shares them so a request retried on another API process, or
after a rolling restart, returns the original result or failure without calling
the provider again.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.models.runtime_state import ReadReplayEntry


@dataclass
class Entry:
    signature: str
    until: float
    future: asyncio.Future


def conflict():
    return ServiceError("REQUEST_ID_CONFLICT", "같은 요청 ID의 내용이 다릅니다.", retryable=False)


def full():
    return ServiceError("SERVICE_UNAVAILABLE", "요청 보관 한도에 도달했습니다.")


def unavailable():
    return ServiceError("DATA_UNAVAILABLE", "요청 재전송 상태를 확인할 수 없습니다.")


def interrupted():
    return ServiceError("PROVIDER_TIMEOUT", "이동시간 확인이 중단되었습니다.")


class ReadReplay:
    def __init__(self, *, capacity=2000, ttl=1800, clock=monotonic):
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.entries = {}

    async def run(self, key, signature, compute, *, model=None):
        self.entries = {
            k: e for k, e in self.entries.items() if e.until > self.clock() or not e.future.done()
        }
        entry = self.entries.get(key)
        if entry is not None:
            if entry.signature != signature:
                raise conflict()
            value, error = await asyncio.shield(entry.future)
            if error:
                raise error
            return value.model_copy(deep=True)
        if len(self.entries) >= self.capacity:
            raise full()
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
            error = interrupted()
            raise
        finally:
            entry.future.set_result((value, error))


@dataclass
class Pending:
    signature: str
    future: asyncio.Future


class PostgresReadReplay:
    """Results and failures shared through PostgreSQL; the database clock decides expiry.

    The first stored outcome for a request ID wins. Concurrent identical requests on
    one process share a single computation; on different processes both may compute,
    but both return the stored winner so the client sees one consistent answer.
    A failed save still returns the computed value because P03 only reads.
    """

    def __init__(self, session_factory, *, capacity=2000, ttl=1800):
        self.session_factory, self.capacity, self.ttl = session_factory, capacity, ttl
        self.pending = {}

    @staticmethod
    def name(key):
        return key if isinstance(key, str) else ":".join(str(part) for part in key)

    @staticmethod
    def error_outcome(error):
        return {
            "kind": "error",
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "retry_after_seconds": error.retry_after_seconds,
        }

    @staticmethod
    def restore(stored_signature, outcome, signature, model):
        if stored_signature != signature:
            raise conflict()
        if outcome["kind"] == "error":
            raise ServiceError(
                outcome["code"],
                outcome["message"],
                retryable=outcome["retryable"],
                retry_after_seconds=outcome["retry_after_seconds"],
            )
        return model.model_validate(outcome["data"])

    async def load(self, name):
        try:
            async with self.session_factory() as session, session.begin():
                return (
                    await session.execute(
                        select(ReadReplayEntry.signature, ReadReplayEntry.outcome).where(
                            ReadReplayEntry.replay_key == name,
                            ReadReplayEntry.expires_at > func.now(),
                        )
                    )
                ).first()
        except SQLAlchemyError:
            raise unavailable() from None

    async def reserve_capacity(self):
        try:
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    delete(ReadReplayEntry).where(ReadReplayEntry.expires_at <= func.now())
                )
                live = await session.scalar(select(func.count()).select_from(ReadReplayEntry))
        except SQLAlchemyError:
            raise unavailable() from None
        if live >= self.capacity:
            raise full()

    async def save(self, name, signature, outcome):
        """Return the stored (signature, outcome): ours if first, otherwise the winner's."""
        statement = insert(ReadReplayEntry).values(
            replay_key=name,
            signature=signature,
            outcome=outcome,
            expires_at=func.now() + timedelta(seconds=self.ttl),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ReadReplayEntry.replay_key],
            set_={
                "signature": statement.excluded.signature,
                "outcome": statement.excluded.outcome,
                "expires_at": statement.excluded.expires_at,
            },
            # Only an expired row is replaced; a live one belongs to the first writer.
            where=ReadReplayEntry.expires_at <= func.now(),
        ).returning(ReadReplayEntry.replay_key)
        async with self.session_factory() as session, session.begin():
            if (await session.execute(statement)).first() is not None:
                return signature, outcome
            winner = (
                await session.execute(
                    select(ReadReplayEntry.signature, ReadReplayEntry.outcome).where(
                        ReadReplayEntry.replay_key == name
                    )
                )
            ).first()
        return winner.signature, winner.outcome

    async def run(self, key, signature, compute, *, model):
        name = self.name(key)
        pending = self.pending.get(name)
        if pending is not None:
            if pending.signature != signature:
                raise conflict()
            value, error = await asyncio.shield(pending.future)
            if error:
                raise error
            return value.model_copy(deep=True)
        # Register before the first await, so a concurrent identical request in this
        # process always joins this one instead of computing again.
        pending = Pending(signature, asyncio.get_running_loop().create_future())
        self.pending[name] = pending
        value, error = None, None
        try:
            stored = await self.load(name)
            if stored is not None:
                value = self.restore(stored.signature, stored.outcome, signature, model)
                return value.model_copy(deep=True)
            await self.reserve_capacity()
            try:
                value = await compute()
            except ServiceError as exc:
                winner = await self.remember_failure(name, signature, exc)
                if winner is None:
                    raise
                # Another process stored first (e.g. a success); answer with that, so
                # one request ID never yields a success on one Pod and a failure here.
                value = self.restore(*winner, signature, model)
                return value.model_copy(deep=True)
            except BaseException:
                # The task may be cancelled; finish the write in a separate task.
                await asyncio.shield(self.remember_failure(name, signature, interrupted()))
                raise
            outcome = {"kind": "value", "data": value.model_dump(mode="json")}
            try:
                saved_signature, saved = await self.save(name, signature, outcome)
            except SQLAlchemyError:
                return value.model_copy(deep=True)
            if saved is not outcome:
                # Another process stored first; everyone returns that answer.
                value = self.restore(saved_signature, saved, signature, model)
            return value.model_copy(deep=True)
        except ServiceError as exc:
            error = exc
            raise
        except BaseException:
            error = interrupted()
            raise
        finally:
            self.pending.pop(name, None)
            pending.future.set_result((value, error))

    async def remember_failure(self, name, signature, error):
        """Store our failure; return the earlier (signature, outcome) if another won."""
        outcome = self.error_outcome(error)
        try:
            saved_signature, saved = await self.save(name, signature, outcome)
        except SQLAlchemyError:
            return None
        return None if saved is outcome else (saved_signature, saved)
