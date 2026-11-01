"""M01/M02 request and layer-specific response models; WGS84 GeoJSON."""

import math
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, field_validator, model_validator

from app.schemas.common import ApiEnvelope, ContractModel, Information

Layer = Literal["roads", "parking", "population"]
StatusLayer = Literal["roads", "population"]
Longitude = Annotated[float, Field(ge=-180, le=180)]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Position = tuple[Longitude, Latitude]
Line = Annotated[list[Position], Field(min_length=2)]


def closed_ring(value):
    if value[0] != value[-1]:
        raise ValueError("Polygon rings must be closed")
    return value


Ring = Annotated[list[Position], Field(min_length=4), AfterValidator(closed_ring)]
PolygonCoordinates = Annotated[list[Ring], Field(min_length=1)]


class MapQuery(ContractModel):
    bbox: tuple[Longitude, Latitude, Longitude, Latitude]
    level: int = Field(gt=0)

    @field_validator("bbox")
    @classmethod
    def ordered_bbox(cls, value):
        if not all(math.isfinite(v) for v in value) or not (
            value[0] < value[2] and value[1] < value[3]
        ):
            raise ValueError("bbox must have west < east and south < north")
        return value


class Point(ContractModel):
    type: Literal["Point"]
    coordinates: Position


class LineString(ContractModel):
    type: Literal["LineString"]
    coordinates: Line


class MultiLineString(ContractModel):
    type: Literal["MultiLineString"]
    coordinates: list[Line] = Field(min_length=1)


class Polygon(ContractModel):
    type: Literal["Polygon"]
    coordinates: PolygonCoordinates


class MultiPolygon(ContractModel):
    type: Literal["MultiPolygon"]
    coordinates: list[PolygonCoordinates] = Field(min_length=1)


class RoadProperties(ContractModel):
    link_id: str
    name: str | None
    direction: str | None


class ParkingProperties(ContractModel):
    parking_id: UUID
    name: str
    identity_status: Literal["verified", "unverified"]


class PopulationProperties(ContractModel):
    area_code: str
    name: str


class Feature[G, P](ContractModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: G
    properties: P

    @model_validator(mode="after")
    def matching_id(self):
        properties = self.properties.model_dump(mode="json")
        key = next(k for k in ("link_id", "parking_id", "area_code") if k in properties)
        if self.id != properties[key]:
            raise ValueError("Feature.id must equal its properties ID")
        return self


class FeatureCollection[T](ContractModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[T]


class GeometryBase(MapQuery):
    geometry_version: str = Field(min_length=1)
    coverage: str


class RoadGeometry(GeometryBase):
    layer: Literal["roads"]
    feature_collection: FeatureCollection[
        Feature[
            Annotated[LineString | MultiLineString, Field(discriminator="type")],
            RoadProperties,
        ]
    ]


class ParkingGeometry(GeometryBase):
    layer: Literal["parking"]
    feature_collection: FeatureCollection[Feature[Point, ParkingProperties]]


class PopulationGeometry(GeometryBase):
    layer: Literal["population"]
    feature_collection: FeatureCollection[
        Feature[
            Annotated[Polygon | MultiPolygon, Field(discriminator="type")],
            PopulationProperties,
        ]
    ]


GeometryData = Annotated[
    RoadGeometry | ParkingGeometry | PopulationGeometry, Field(discriminator="layer")
]


class GeometryResponse(ApiEnvelope[GeometryData]):
    data: GeometryData
    error: None = None


class TrafficValue(ContractModel):
    speed_kmh: float | None = Field(ge=0)
    travel_seconds: float | None = Field(ge=0)


class PopulationValue(ContractModel):
    min_people: int | None = Field(ge=0, strict=True)
    max_people: int | None = Field(ge=0, strict=True)
    congestion: Literal["relaxed", "normal", "slightly_crowded", "crowded"] | None
    is_replacement: bool | None = Field(strict=True)


class RoadStatusItem(ContractModel):
    link_id: str
    traffic: Information[TrafficValue]


class PopulationStatusItem(ContractModel):
    area_code: str
    population: Information[PopulationValue]


class StatusBase(MapQuery):
    geometry_version: str = Field(min_length=1)
    status: Literal["ok", "partial"]


class RoadStatus(StatusBase):
    layer: Literal["roads"]
    items: list[RoadStatusItem]


class PopulationStatus(StatusBase):
    layer: Literal["population"]
    items: list[PopulationStatusItem]


StatusData = Annotated[RoadStatus | PopulationStatus, Field(discriminator="layer")]


class StatusResponse(ApiEnvelope[StatusData]):
    data: StatusData
    error: None = None
