"""Immutable condition versions, serialized by the trip's current-version row.

The caller owns the write transaction and must commit before publishing state.
No provider calls, search progress, request replay or result writes live here.
"""

from dataclasses import dataclass
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChatConditionAgeGroup,
    ChatConditionPurpose,
    ChatConditionVersion,
    ChatSession,
)
from app.schemas.location import Coordinates, Location
from app.schemas.recommendations import Conditions


class ConditionVersionNotFound(LookupError):
    """The requested trip or historical condition version is not stored."""


class ConditionVersionMismatch(ValueError):
    """The request's base version or values differ from the stored conditions."""


class ConditionValuesInvalid(ValueError):
    """Conditions cannot be stored without losing their original meaning."""


class ConditionSessionChanged(LookupError):
    """Trip existence changed after preflight; memory no longer owns its progress."""


@dataclass(frozen=True)
class ConditionState:
    version: int
    conditions: Conditions


def _validate_values(conditions: Conditions) -> None:
    purposes = [p.purpose for p in conditions.purposes]
    ages = conditions.age_groups
    if len(purposes) != len(set(purposes)) or (
        ages is not None and (not ages or len(ages) != len(set(ages)))
    ):
        # No child rows means unspecified ages (None), never an empty list.
        raise ConditionValuesInvalid("Purpose/age values must be unique; unspecified ages use None")


class RecommendationConditionsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_current(self, trip_id: UUID) -> ConditionState | None:
        """Read the current pointer and its immutable values; readers use one DB snapshot."""
        version = await self.session.scalar(
            select(ChatSession.condition_version).where(ChatSession.trip_id == trip_id)
        )
        if version is None:
            return None
        return ConditionState(version, await self.get_version(trip_id, version))

    async def accept(
        self,
        trip_id: UUID,
        base_version: int,
        base_conditions: Conditions,
        conditions: Conditions,
        *,
        expected_existing: bool | None = None,
    ) -> ConditionState:
        """Compare the authoritative base, append a changed version, and advance its pointer.

        Only a new trip at version 0 may create a baseline. Concurrent first
        inserts wait on the unique trip key, then re-read the winner under
        READ COMMITTED. Existing trips serialize on SELECT FOR UPDATE. Old
        versions and children are never updated, even when reverting values.
        """
        if not self.session.in_transaction():
            raise InvalidRequestError("Accept requires a caller-owned transaction")
        if type(base_version) is not int or base_version < 0:
            raise ValueError("The base condition version must be a nonnegative integer")
        base, accepted = base_conditions.model_copy(deep=True), conditions.model_copy(deep=True)
        _validate_values(base)
        _validate_values(accepted)
        async with self.session.begin_nested():
            current = await self._lock_current(trip_id)
            created = False
            if current is None:
                if expected_existing is True:
                    raise ConditionSessionChanged("The previously stored trip is missing")
                if base_version != 0:
                    raise ConditionVersionNotFound("Only version 0 can start a new trip")
                created = (
                    await self.session.scalar(
                        insert(ChatSession)
                        .values(trip_id=trip_id, condition_version=0)
                        .on_conflict_do_nothing(index_elements=[ChatSession.trip_id])
                        .returning(ChatSession.trip_id)
                    )
                ) is not None
                # A conflicting insert may have committed a newer version while
                # this request waited. Always read again before comparing values.
                current = await self._lock_current(trip_id)
            if expected_existing is False and not created:
                raise ConditionSessionChanged("Another execution already created this trip")
            if current != base_version:
                raise ConditionVersionMismatch("The trip has a different current version")
            if created:
                await self._insert_version(trip_id, 0, base)
            elif await self.get_version(trip_id, base_version) != base:
                raise ConditionVersionMismatch("Stored condition version has different values")
            version = base_version + int(accepted != base)
            if version != base_version:
                await self._insert_version(trip_id, version, accepted)
                await self.session.execute(
                    update(ChatSession)
                    .where(ChatSession.trip_id == trip_id)
                    # A waiting transaction can predate the session's creation.
                    # PostgreSQL now() would then violate updated_at >= created_at.
                    .values(condition_version=version, updated_at=func.clock_timestamp())
                )
        return ConditionState(version, accepted)

    async def _lock_current(self, trip_id: UUID) -> int | None:
        return await self.session.scalar(
            select(ChatSession.condition_version)
            .where(ChatSession.trip_id == trip_id)
            .with_for_update()
        )

    async def _insert_version(self, trip_id: UUID, version: int, conditions: Conditions) -> None:
        origin = conditions.origin
        await self.session.execute(
            ChatConditionVersion.__table__.insert().values(
                trip_id=trip_id,
                version=version,
                origin_label=origin.label if origin else None,
                origin_location=(
                    f"SRID=4326;POINT({origin.coordinates.longitude} {origin.coordinates.latitude})"
                    if origin
                    else None
                ),
                origin_address=origin.address if origin else None,
                origin_region=origin.region if origin else None,
                origin_supported=origin.supported if origin else None,
                group_type=conditions.group_type,
                headcount=conditions.headcount,
                max_travel_seconds=conditions.max_travel_seconds,
            )
        )
        purposes = [
            dict(trip_id=trip_id, condition_version=version, position=i, **p.model_dump())
            for i, p in enumerate(conditions.purposes)
        ]
        if purposes:
            await self.session.execute(ChatConditionPurpose.__table__.insert(), purposes)
        ages = [
            dict(trip_id=trip_id, condition_version=version, position=i, age_group=age)
            for i, age in enumerate(conditions.age_groups or [])
        ]
        if ages:
            await self.session.execute(ChatConditionAgeGroup.__table__.insert(), ages)

    async def get_version(self, trip_id: UUID, version: int, *, lock=False) -> Conditions:
        table = ChatConditionVersion.__table__
        point = cast(table.c.origin_location, Geometry("POINT", srid=4326))
        statement = select(
            table, func.ST_X(point).label("longitude"), func.ST_Y(point).label("latitude")
        ).where(table.c.trip_id == trip_id, table.c.version == version)
        if lock:
            statement = statement.with_for_update(read=True)
        rows = await self.session.execute(statement)
        row = rows.mappings().one_or_none()
        if row is None:
            raise ConditionVersionNotFound("The accepted condition version is not stored")

        purposes = await self._children(ChatConditionPurpose, trip_id, version, lock=lock)
        ages = await self._children(ChatConditionAgeGroup, trip_id, version, lock=lock)
        origin = None
        if row["origin_label"] is not None:
            origin = Location(
                label=row["origin_label"],
                coordinates=Coordinates(latitude=row["latitude"], longitude=row["longitude"]),
                address=row["origin_address"],
                region=row["origin_region"],
                supported=row["origin_supported"],
            )
        return Conditions(
            origin=origin,
            purposes=[{"purpose": p["purpose"], "keyword": p["keyword"]} for p in purposes],
            group_type=row["group_type"],
            age_groups=[age["age_group"] for age in ages] or None,
            headcount=row["headcount"],
            max_travel_seconds=row["max_travel_seconds"],
        )

    async def _children(self, model, trip_id, version, *, lock):
        statement = (
            select(model.__table__)
            .where(model.trip_id == trip_id, model.condition_version == version)
            .order_by(model.position)
        )
        if lock:
            statement = statement.with_for_update(read=True)
        return (await self.session.execute(statement)).mappings().all()
