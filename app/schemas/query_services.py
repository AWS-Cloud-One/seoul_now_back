"""Part 1 provisional independent query contracts (no recommendation context)."""

from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, field_validator

from app.schemas.common import ApiEnvelope, ContractModel, Information
from app.schemas.location import Coordinates
from app.schemas.map import MultiLineString
from app.schemas.parking_nearby import ParkingFacility


class DrivingQuery(ContractModel):
    origin_latitude: float = Field(ge=-90, le=90)
    origin_longitude: float = Field(ge=-180, le=180)
    destination_latitude: float = Field(ge=-90, le=90)
    destination_longitude: float = Field(ge=-180, le=180)
    include_geometry: bool = True


class DrivingRoute(ContractModel):
    duration_seconds: int = Field(strict=True, ge=0)
    distance_meters: float = Field(ge=0)
    geometry: MultiLineString | None
    geometry_reason: str | None
    taxi_fare_krw: int | None = Field(strict=True, ge=0)
    toll_fare_krw: int | None = Field(strict=True, ge=0)


class DrivingData(ContractModel):
    origin: Coordinates
    destination: Coordinates
    checked_at: AwareDatetime
    route: Information[DrivingRoute]


class DrivingResponse(ApiEnvelope[DrivingData]):
    pass


class PlaceSearchQuery(ContractModel):
    query: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=10, ge=1, le=20)
    cursor: str | None = None


class NearbyPlaceQuery(Coordinates):
    category: Literal["food", "cafe"]
    radius_meters: int = Field(default=800, ge=1, le=20000)
    limit: int = Field(default=10, ge=1, le=20)
    cursor: str | None = None


class FoundPlace(ContractModel):
    place_id: str
    name: str
    category_code: str | None
    category_name: str | None
    coordinates: Coordinates
    address: str | None
    road_address: str | None
    phone: str | None
    provider_url: str | None
    distance_meters: float | None = Field(ge=0)
    source: Literal["kakao_local"] = "kakao_local"
    collected_at: AwareDatetime


class PlaceSearchData(ContractModel):
    items: list[FoundPlace]
    next_cursor: str | None
    has_more: bool


class PlaceSearchResponse(ApiEnvelope[PlaceSearchData]):
    pass


class ParkingSearchQuery(Coordinates):
    radius_meters: Literal[500, 1000, 2000] = 1000
    name: str | None = Field(default=None, max_length=100)
    sort: Literal["distance", "price"] = "distance"
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None

    @field_validator("radius_meters", mode="before")
    @classmethod
    def radius_from_query(cls, value):
        return int(value) if isinstance(value, str) and value in {"500", "1000", "2000"} else value


class ParkingSearchItem(ContractModel):
    facility: ParkingFacility
    hourly_base_comparison_krw: float | None = Field(ge=0)
    fee_status: Literal["free", "paid", "unknown", "conflict"]
    fee_reason: str | None


class ParkingSearchData(ContractModel):
    checked_at: AwareDatetime
    radius_meters: int
    total_count: int = Field(ge=0)
    items: list[ParkingSearchItem]
    next_cursor: str | None


class ParkingSearchResponse(ApiEnvelope[ParkingSearchData]):
    pass


class AreaOverviewData(ContractModel):
    area_code: str
    name: str
    scope: Literal["population_observation_area"]
    checked_at: AwareDatetime
    population: Information[JsonValue]
    roads: Information[JsonValue]
    parking: Information[JsonValue]
    weather: Information[JsonValue]
    commerce: Information[JsonValue]


class AreaOverviewResponse(ApiEnvelope[AreaOverviewData]):
    pass
