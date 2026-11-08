"""Insert-only historical results, independent of provider caches and token policy.

Writers call save() inside an explicit transaction, e.g. write_factory.begin().
The repository never commits: its savepoint rolls back a failed aggregate even
when a caller catches the error and continues the surrounding transaction.
Readers should use read_session_factory for one consistent database snapshot.
Returned model instances are independent copies, never tracked for ORM updates.
"""

from dataclasses import dataclass
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import cast, func, inspect, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChatConditionAgeGroup,
    ChatConditionPurpose,
    ChatConditionVersion,
    RecommendationCard,
    RecommendationEvidence,
    RecommendationResult,
    RecommendationSearchArea,
)
from app.schemas.location import Coordinates, Location
from app.schemas.recommendations import Conditions, Purpose


class ConditionVersionNotFound(LookupError):
    """The condition owner must persist the accepted version before its result."""


class ConditionVersionMismatch(ValueError):
    """An existing version cannot be reused with different condition values."""


@dataclass(frozen=True)
class RecommendationSnapshot:
    result: RecommendationResult
    conditions: Conditions
    cards: tuple[RecommendationCard, ...]
    evidence: tuple[RecommendationEvidence, ...]
    search_areas: tuple[RecommendationSearchArea, ...] = ()
    # Derived from the saved geography on read; never from the mutable place cache.
    card_coordinates: tuple[Coordinates, ...] = ()


def _values(row):
    if not inspect(row).transient:
        raise ValueError("Snapshot rows must be independent, unsaved model instances")
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def _validate_membership(snapshot: RecommendationSnapshot) -> None:
    result, cards = snapshot.result, snapshot.cards
    if result.result_id is None:
        raise ValueError("A snapshot requires an explicit result_id")
    if not 1 <= len(cards) <= 3 or sorted(c.position for c in cards) != list(range(len(cards))):
        raise ValueError("A snapshot requires 1 to 3 cards with consecutive positions")
    places = {card.place_id for card in cards}
    if len(places) != len(cards):
        raise ValueError("A snapshot cannot contain duplicate places")
    for row in (*cards, *snapshot.evidence):
        if row.result_id != result.result_id or row.result_kind != result.kind:
            raise ValueError("Snapshot rows must belong to the same result and kind")
    positions = {place: [] for place in places}
    for item in snapshot.evidence:
        if item.place_id not in places:
            raise ValueError("Evidence must belong to a card in this snapshot")
        positions[item.place_id].append(item.position)
    if any(sorted(items) != list(range(len(items))) for items in positions.values()):
        raise ValueError("Evidence positions must be consecutive within each card")
    areas = snapshot.search_areas
    if (
        any(area.result_id != result.result_id for area in areas)
        or sorted(a.position for a in areas) != list(range(len(areas)))
        or len({a.area_id for a in areas}) != len(areas)
        or (areas and result.search_fallback is not False)
        or (not areas and result.search_fallback is False)
    ):
        raise ValueError("Search areas must match the result's ordered coverage")


class RecommendationContextRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def save(self, snapshot: RecommendationSnapshot) -> None:
        """Insert one aggregate; neither create conditions nor overwrite existing results.

        Token policy, retry eligibility and evidence/fact agreement belong to the
        context service. This boundary checks membership and the persisted version.
        """
        if not self.session.in_transaction():
            raise InvalidRequestError("Save requires a caller-owned transaction")
        _validate_membership(snapshot)
        result = _values(snapshot.result)
        cards = [_values(card) for card in snapshot.cards]
        evidence = [_values(item) for item in snapshot.evidence]
        areas = [_values(area) for area in snapshot.search_areas]
        expected = snapshot.conditions.model_copy(deep=True)
        async with self.session.begin_nested():
            stored = await self._conditions(
                result["trip_id"], result["condition_version"], lock=True
            )
            if stored != expected:
                raise ConditionVersionMismatch("Stored condition version has different values")
            # Core inserts keep the caller's objects out of the session identity map.
            # Explicit parent-first order also avoids relying on ORM relationships.
            await self.session.execute(RecommendationResult.__table__.insert(), result)
            await self.session.execute(RecommendationCard.__table__.insert(), cards)
            if evidence:
                await self.session.execute(RecommendationEvidence.__table__.insert(), evidence)
            if areas:
                await self.session.execute(RecommendationSearchArea.__table__.insert(), areas)

    async def get(self, trip_id: UUID, result_id: UUID) -> RecommendationSnapshot | None:
        """Read stored facts in display order, scoped to the owning trip.

        Expired results remain readable internally; the context service enforces
        token/expiry checks before exposing anything to an API caller.
        """
        results = await self.session.execute(
            select(RecommendationResult.__table__).where(
                RecommendationResult.trip_id == trip_id,
                RecommendationResult.result_id == result_id,
            )
        )
        result = results.mappings().one_or_none()
        if result is None:
            return None
        conditions = await self._conditions(trip_id, result["condition_version"])
        location = cast(RecommendationCard.place_location, Geometry("POINT", srid=4326))
        cards = await self.session.execute(
            select(
                RecommendationCard.__table__,
                func.ST_X(location).label("longitude"),
                func.ST_Y(location).label("latitude"),
            )
            .where(RecommendationCard.result_id == result_id)
            .order_by(RecommendationCard.position)
        )
        evidence = await self.session.execute(
            select(RecommendationEvidence.__table__)
            .join(
                RecommendationCard,
                (RecommendationEvidence.result_id == RecommendationCard.result_id)
                & (RecommendationEvidence.place_id == RecommendationCard.place_id),
            )
            .where(RecommendationEvidence.result_id == result_id)
            .order_by(RecommendationCard.position, RecommendationEvidence.position)
        )
        areas = await self.session.execute(
            select(RecommendationSearchArea.__table__)
            .where(RecommendationSearchArea.result_id == result_id)
            .order_by(RecommendationSearchArea.position)
        )
        card_rows, coordinates = [], []
        for row in cards.mappings():
            values = dict(row)
            coordinates.append(
                Coordinates(longitude=values.pop("longitude"), latitude=values.pop("latitude"))
            )
            card_rows.append(RecommendationCard(**values))
        return RecommendationSnapshot(
            result=RecommendationResult(**result),
            conditions=conditions,
            cards=tuple(card_rows),
            evidence=tuple(RecommendationEvidence(**row) for row in evidence.mappings()),
            search_areas=tuple(RecommendationSearchArea(**row) for row in areas.mappings()),
            card_coordinates=tuple(coordinates),
        )

    async def get_by_token_digest(self, digest: str) -> RecommendationSnapshot | None:
        """Resolve P01's opaque header without requiring public trip/result identifiers."""
        rows = await self.session.execute(
            select(RecommendationResult.trip_id, RecommendationResult.result_id).where(
                RecommendationResult.context_token_digest == digest
            )
        )
        identity = rows.one_or_none()
        if identity is None:
            return None
        return await self.get(identity.trip_id, identity.result_id)

    async def _conditions(self, trip_id: UUID, version: int, *, lock=False) -> Conditions:
        table = ChatConditionVersion.__table__
        point = cast(table.c.origin_location, Geometry("POINT", srid=4326))
        statement = select(
            table, func.ST_X(point).label("longitude"), func.ST_Y(point).label("latitude")
        ).where(table.c.trip_id == trip_id, table.c.version == version)
        if lock:
            # Condition versions are immutable; cooperating condition writers must
            # lock this parent before modifying its purpose/age children as well.
            statement = statement.with_for_update(read=True)
        rows = await self.session.execute(statement)
        row = rows.mappings().one_or_none()
        if row is None:
            raise ConditionVersionNotFound("The accepted condition version is not stored")

        purposes = await self._condition_children(ChatConditionPurpose, trip_id, version, lock=lock)
        ages = await self._condition_children(ChatConditionAgeGroup, trip_id, version, lock=lock)
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
            purposes=[Purpose(purpose=p["purpose"], keyword=p["keyword"]) for p in purposes],
            group_type=row["group_type"],
            # Accepted conditions use None for unspecified ages, never an empty list.
            age_groups=[age["age_group"] for age in ages] or None,
            headcount=row["headcount"],
            max_travel_seconds=row["max_travel_seconds"],
        )

    async def _condition_children(self, model, trip_id, version, *, lock):
        statement = (
            select(model.__table__)
            .where(model.trip_id == trip_id, model.condition_version == version)
            .order_by(model.position)
        )
        if lock:
            statement = statement.with_for_update(read=True)
        return (await self.session.execute(statement)).mappings().all()
