"""Typed, rebuildable publications. No provider JSON or polymorphic value object."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .areas import population_checks
from .base import Base, nonblank, nonnegative


class MapLayerVersion(Base):
    __tablename__ = "map_layer_versions"
    __table_args__ = (
        *nonblank("geometry_version"),
        CheckConstraint("layer IN ('roads','parking','population')", name="layer"),
        CheckConstraint(
            "NOT is_ready OR (geometry_version IS NOT NULL AND btrim(geometry_version) <> '')",
            name="ready_version",
        ),
    )

    layer: Mapped[str] = mapped_column(Text, primary_key=True)
    geometry_version: Mapped[str | None] = mapped_column(Text)
    is_ready: Mapped[bool] = mapped_column(Boolean, server_default="false")
    coverage: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MapFeature(Base):
    __tablename__ = "map_features"
    __table_args__ = (
        *nonblank("feature_id", "name"),
        CheckConstraint("NOT ST_IsEmpty(geometry) AND ST_IsValid(geometry)", name="valid_geometry"),
        CheckConstraint(
            "(layer = 'roads' AND road_link_id IS NOT NULL AND road_link_id = feature_id "
            "AND population_area_code IS NULL AND parking_id IS NULL "
            "AND identity_status IS NULL AND ST_GeometryType(geometry) = 'ST_MultiLineString') OR "
            "(layer = 'population' AND population_area_code IS NOT NULL "
            "AND population_area_code = feature_id "
            "AND road_link_id IS NULL AND parking_id IS NULL AND name IS NOT NULL "
            "AND identity_status IS NULL AND direction IS NULL "
            "AND ST_GeometryType(geometry) = 'ST_MultiPolygon') OR "
            "(layer = 'parking' AND parking_id IS NOT NULL AND parking_id::text = feature_id "
            "AND road_link_id IS NULL AND population_area_code IS NULL AND name IS NOT NULL "
            "AND direction IS NULL AND identity_status IS NOT NULL "
            "AND identity_status IN ('verified','unverified') "
            "AND ST_GeometryType(geometry) = 'ST_Point')",
            name="layer_identity_geometry",
        ),
    )

    layer: Mapped[str] = mapped_column(
        Text, ForeignKey("map_layer_versions.layer", ondelete="RESTRICT"), primary_key=True
    )
    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    road_link_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("road_links.link_id", ondelete="RESTRICT")
    )
    population_area_code: Mapped[str | None] = mapped_column(
        Text, ForeignKey("population_areas.area_code", ondelete="RESTRICT")
    )
    parking_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("parking_facilities.parking_id", ondelete="RESTRICT")
    )
    geometry = mapped_column(Geometry("GEOMETRY", srid=4326), nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str | None] = mapped_column(Text)
    identity_status: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PublishedObservation:
    source: Mapped[str | None] = mapped_column(
        Text, ForeignKey("data_sources.source_code", ondelete="RESTRICT")
    )
    payload_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT"), index=True
    )
    has_observation: Mapped[bool] = mapped_column(Boolean, server_default="false")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processor_version: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    last_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("collection_runs.id", ondelete="RESTRICT"), index=True
    )
    collection_failed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def published_checks(layer, identity, values):
    return (
        CheckConstraint(f"layer = '{layer}'", name="layer"),
        ForeignKeyConstraint(
            ["layer", identity],
            ["map_features.layer", "map_features.feature_id"],
            ondelete="RESTRICT",
        ),
        *nonblank("processor_version"),
        CheckConstraint(
            "NOT has_observation OR (payload_id IS NOT NULL AND source IS NOT NULL "
            "AND processor_version IS NOT NULL AND processed_at IS NOT NULL)",
            name="observation_evidence",
        ),
        CheckConstraint(
            "has_observation OR (" + " AND ".join(f"{v} IS NULL" for v in values) + ")",
            name="absent_values",
        ),
        CheckConstraint("(payload_id IS NULL) = (source IS NULL)", name="source_payload_pair"),
    )


class MapRoadObservation(PublishedObservation, Base):
    __tablename__ = "map_road_observations"
    __table_args__ = (
        *published_checks("roads", "link_id", ("speed_kmh", "travel_seconds")),
        *nonnegative("speed_kmh", "travel_seconds"),
    )

    link_id: Mapped[str] = mapped_column(Text, primary_key=True)
    layer: Mapped[str] = mapped_column(Text, server_default="roads")
    speed_kmh: Mapped[Decimal | None] = mapped_column(Numeric)
    travel_seconds: Mapped[Decimal | None] = mapped_column(Numeric)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("collection_runs.id", ondelete="SET NULL"), index=True
    )


class MapPopulationObservation(PublishedObservation, Base):
    __tablename__ = "map_population_observations"
    __table_args__ = (
        *published_checks(
            "population", "area_code", ("min_people", "max_people", "congestion", "is_replacement")
        ),
        *population_checks(),
    )

    area_code: Mapped[str] = mapped_column(Text, primary_key=True)
    layer: Mapped[str] = mapped_column(Text, server_default="population")
    min_people: Mapped[int | None] = mapped_column(Integer)
    max_people: Mapped[int | None] = mapped_column(Integer)
    congestion: Mapped[str | None] = mapped_column(Text)
    is_replacement: Mapped[bool | None] = mapped_column(Boolean)
