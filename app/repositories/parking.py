from datetime import UTC, datetime, timedelta

from geoalchemy2 import Geometry
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.parking import CollectionRun, ParkingRecord
from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    MUNICIPAL_PARKING_SOURCE,
)

LIVE_STATUS_MAX_AGE = timedelta(minutes=15)


def parking_availability(
    source: str,
    details: dict | None,
    source_updated_at: datetime | None,
    now: datetime | None = None,
) -> dict:
    """Expose a vacancy count only when Seoul says it is real-time and fresh."""
    result = {
        "current_availability": "unconfirmed",
        "availability_status": "unconfirmed",
        "available_spaces": None,
        "availability_updated_at": None,
        "real_time_available": False,
    }
    if source not in {CITYDATA_PARKING_SOURCE, MUNICIPAL_PARKING_SOURCE}:
        return result

    details = details or {}
    flag = str(details.get("CUR_PRK_YN") or "").strip().upper()
    if flag == "N":
        result.update(current_availability="not_provided", availability_status="not_provided")
        return result
    if flag != "Y":
        return result
    try:
        count = int(str(details.get("CUR_PRK_CNT")).strip())
    except TypeError, ValueError:
        return result
    if count < 0 or source_updated_at is None:
        return result

    current = now or datetime.now(UTC)
    observed = source_updated_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    age = current - observed
    if age < timedelta(0) or age > LIVE_STATUS_MAX_AGE:
        result.update(current_availability="stale", availability_status="stale")
        return result

    result.update(
        current_availability="available",
        availability_status="available",
        available_spaces=count,
        availability_updated_at=source_updated_at,
        real_time_available=True,
    )
    return result


class ParkingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, rows: list[dict]) -> None:
        # Batches avoid PostgreSQL's bound-parameter limit.
        for offset in range(0, len(rows), 500):
            statement = insert(ParkingRecord).values(rows[offset : offset + 500])
            statement = statement.on_conflict_do_update(
                index_elements=[ParkingRecord.id],
                set_={key: getattr(statement.excluded, key) for key in rows[0] if key != "id"},
                where=ParkingRecord.collected_at < statement.excluded.collected_at,
            )
            await self.session.execute(statement)

    async def nearby(self, lat: float, lon: float, source: str, limit: int) -> dict:
        point = func.ST_GeogFromText(f"SRID=4326;POINT({lon} {lat})")
        distance = func.ST_Distance(ParkingRecord.location, point)
        latest = await self.session.scalar(
            select(CollectionRun)
            .where(
                CollectionRun.source == source,
                CollectionRun.status.in_(["success", "partial"]),
            )
            .order_by(CollectionRun.started_at.desc())
            .limit(1)
        )
        if latest is None:
            return {"status": "unconfirmed", "radius_m": None, "items": []}
        latest_attempt = await self.session.scalar(
            select(CollectionRun)
            .where(CollectionRun.source == source)
            .order_by(CollectionRun.started_at.desc())
            .limit(1)
        )
        age = (datetime.now(latest.started_at.tzinfo) - latest.started_at).total_seconds()
        citydata = source in {CITYDATA_PARKING_SOURCE, MUNICIPAL_PARKING_SOURCE}
        fallback: tuple[int, list[dict]] | None = None
        for radius in (300, 500, 1000):
            result = await self.session.execute(
                select(
                    ParkingRecord,
                    distance.label("distance_m"),
                    func.ST_Y(ParkingRecord.location.cast(Geometry)),
                    func.ST_X(ParkingRecord.location.cast(Geometry)),
                )
                .where(
                    ParkingRecord.source == source,
                    ParkingRecord.collected_at == latest.started_at,
                    func.ST_DWithin(ParkingRecord.location, point, radius),
                )
                .order_by(distance, ParkingRecord.id)
                .limit(max(limit, 100) if citydata else limit)
            )
            items = []
            for record, meters, latitude, longitude in result:
                items.append(
                    {
                        "id": record.id,
                        "source": record.source,
                        "source_id": record.source_id,
                        "name": record.name,
                        "address": record.address,
                        "lat": latitude,
                        "lon": longitude,
                        "distance_m": round(meters, 1),
                        "registered_details": record.details,
                        "source_updated_at": record.source_updated_at,
                        "collected_at": record.collected_at,
                        **parking_availability(
                            record.source, record.details, record.source_updated_at
                        ),
                    }
                )
            if not items:
                continue
            if citydata:
                items.sort(key=lambda item: (not item["real_time_available"], item["distance_m"]))
                items = items[:limit]
            fallback = (radius, items)
            if not citydata or any(item["real_time_available"] for item in items):
                break
        if fallback is None:
            return {
                "status": "unconfirmed",
                "radius_m": 1000,
                "items": [],
                "notice": "보유 자료에서 확인 불가. 실제 주차장이 없다는 뜻은 아닙니다.",
            }
        radius, items = fallback
        has_live = any(item["real_time_available"] for item in items)
        return {
            "status": "live_parking"
            if citydata and has_live
            else (
                "real_time_unavailable"
                if citydata
                else ("delayed" if age > 7 * 86400 else "registered_information")
            ),
            "radius_m": radius,
            "items": items,
            "collection_status": latest.status,
            "latest_attempt_status": latest_attempt.status,
            "latest_attempt_error": latest_attempt.error_code,
            "source": source,
            "is_demo": source.startswith("demo_"),
            "last_collected_at": latest.started_at,
            "notice": (
                "실시간 제공 표식과 원천 갱신 시각(15분 이내)을 확인한 "
                "항목만 주차 가능 면수를 표시합니다. "
                "그 외는 실시간 미제공 또는 미확인입니다."
            )
            if citydata
            else "등록 기본정보·직선거리이며 현재 요금·빈자리를 보증하지 않습니다.",
        }
