"""P02 first-record radius lookup with bound query/version pagination."""

from datetime import UTC, datetime

from pydantic import ValidationError

from app.clients.kakao import KakaoError, KakaoLocalClient
from app.core.exceptions import ServiceError
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.schemas.location import Coordinates
from app.schemas.parking_nearby import NearbyParkingData, ParkingFacility, ParkingSummary
from app.services.locations import provider_failure, region_from_coordinates
from app.services.parking_registration import registered_information
from app.services.query_cursors import QUERY_CURSORS


class ParkingNearbyService:
    def __init__(
        self, repository: ParkingNearbyRepository, provider: KakaoLocalClient, *, cursors=None
    ):
        self.repository, self.provider = repository, provider
        self.cursors = cursors if cursors is not None else QUERY_CURSORS

    async def search(
        self,
        coordinates: Coordinates,
        limit: int,
        cursor: str | None,
        *,
        verified_seoul=False,
    ):
        scope = ("parking-nearby", coordinates.latitude, coordinates.longitude, limit)
        page = self.cursors.read(cursor, scope) if cursor else None
        try:
            # Only internal readers of authenticated Seoul result coordinates may skip this.
            if not verified_seoul:
                region = region_from_coordinates(
                    await self.provider.region(
                        coordinates.latitude,
                        coordinates.longitude,
                    )
                )
                if region != "seoul":
                    raise ServiceError("VALIDATION_ERROR", "서울 목적지 좌표를 입력해 주세요.")
            now = datetime.now(UTC)
            version = await self.repository.get_version()
            summary = ParkingSummary(
                status="unavailable",
                reason_code="NO_RECORD",
                checked_at=now,
                search_radius_meters=None,
                nearest_distance_meters=None,
                items=[],
                total_count=None,
                next_cursor=None,
            )
            if version is None or not version.is_ready or not version.geometry_version:
                if page is not None:
                    raise ServiceError(
                        "CURSOR_EXPIRED", "주차 자료가 변경됐습니다. 첫 페이지부터 조회해 주세요."
                    )
                return NearbyParkingData(coordinates=coordinates, parking=summary)
            if page and page["version"] != version.geometry_version:
                raise ServiceError("CURSOR_EXPIRED", "주차 자료 버전이 변경됐습니다.")
            for radius in (page["radius"],) if page else (300, 500, 1000):
                count, nearest = await self.repository.summarize_radius(coordinates, radius)
                if page and count != page["count"]:
                    raise ServiceError("CURSOR_EXPIRED", "주차 조회 대상이 변경됐습니다.")
                if count == 0:
                    continue
                offset = page["offset"] if page else 0
                rows = await self.repository.list_facilities(
                    coordinates, radius, limit=limit, offset=offset
                )
                if len(rows) != min(limit, count - offset):
                    raise ServiceError("DATA_UNAVAILABLE", "주차 조회 자료가 변경되었습니다.")
                records = await self.repository.list_records([row[0] for row in rows])
                items = [
                    ParkingFacility(
                        parking_id=key,
                        identity_status=identity,
                        name=props["name"],
                        coordinates=Coordinates(latitude=lat, longitude=lon),
                        distance_meters=meters,
                        records=[
                            {
                                "source_record_id": r.source_record_id,
                                "source": r.source,
                                "reference_date": r.reference_date,
                                "registered": registered_information(r),
                            }
                            for r in records.get(key, [])
                        ],
                    )
                    for key, identity, props, lat, lon, meters in rows
                ]
                return NearbyParkingData(
                    coordinates=coordinates,
                    parking=ParkingSummary(
                        status="available",
                        reason_code=None,
                        checked_at=now,
                        search_radius_meters=radius,
                        nearest_distance_meters=nearest,
                        items=items,
                        total_count=count,
                        next_cursor=self.cursors.issue(
                            scope,
                            {
                                "radius": radius,
                                "version": version.geometry_version,
                                "count": count,
                                "offset": offset + len(rows),
                            },
                        )
                        if offset + len(rows) < count
                        else None,
                    ),
                )
            summary.search_radius_meters, summary.total_count = 1000, 0
            return NearbyParkingData(coordinates=coordinates, parking=summary)
        except KakaoError as exc:
            raise provider_failure(exc) from None
        except ValidationError, KeyError, TypeError:
            raise ServiceError("DATA_UNAVAILABLE", "주차 등록정보를 확인할 수 없습니다.") from None
