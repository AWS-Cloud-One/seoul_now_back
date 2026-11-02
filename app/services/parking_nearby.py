"""P02 first-record radius policy; cursor storage/expiry is still pending."""

from datetime import UTC, datetime

from pydantic import ValidationError

from app.api.contract import ContractError
from app.clients.kakao import KakaoError, KakaoLocalClient
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.schemas.location import Coordinates
from app.schemas.parking_nearby import NearbyFacility, NearbyParkingData, ParkingSummary
from app.services.locations import provider_failure, region_from_coordinates


class ParkingNearbyService:
    def __init__(self, repository: ParkingNearbyRepository, provider: KakaoLocalClient):
        self.repository, self.provider = repository, provider

    async def search(self, coordinates: Coordinates, limit: int, cursor: str | None):
        if cursor is not None:
            raise self.pagination_unavailable()
        try:
            region = region_from_coordinates(
                await self.provider.region(
                    coordinates.latitude,
                    coordinates.longitude,
                )
            )
            if region != "seoul":
                raise ContractError(422, "VALIDATION_ERROR", "서울 목적지 좌표를 입력해 주세요.")
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
                return NearbyParkingData(coordinates=coordinates, parking=summary)
            for radius in (300, 500, 1000):
                count, nearest = await self.repository.summarize_radius(coordinates, radius)
                if count == 0:
                    continue
                if count > limit:
                    # Do not enlarge the radius or pretend an incomplete page is final.
                    raise self.pagination_unavailable()
                rows = await self.repository.list_facilities(coordinates, radius)
                if len(rows) != count:
                    raise ContractError(503, "DATA_UNAVAILABLE", "주차 조회 자료가 변경되었습니다.")
                records = await self.repository.list_records([row[0] for row in rows])
                items = [
                    NearbyFacility(
                        parking_id=key,
                        identity_status=identity,
                        name=props["name"],
                        coordinates=Coordinates(latitude=lat, longitude=lon),
                        distance_meters=meters,
                        records=[
                            {
                                "source_record_id": r.id,
                                "source": r.source,
                                "reference_date": r.reference_date,
                                "registered": r.registered,
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
                        next_cursor=None,
                    ),
                )
            summary.search_radius_meters, summary.total_count = 1000, 0
            return NearbyParkingData(coordinates=coordinates, parking=summary)
        except KakaoError as exc:
            raise provider_failure(exc) from None
        except ValidationError, KeyError, TypeError:
            raise ContractError(
                503, "DATA_UNAVAILABLE", "주차 등록정보를 확인할 수 없습니다."
            ) from None

    @staticmethod
    def pagination_unavailable():
        return ContractError(
            503,
            "SERVICE_UNAVAILABLE",
            "주변 주차 다음 페이지 처리가 아직 연결되지 않았습니다.",
            retryable=False,
        )
