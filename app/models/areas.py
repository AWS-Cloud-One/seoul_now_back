"""Distinct hotspot, administrative and regional boundaries; typed population values."""

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, CheckConstraint, Double, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import (
    AuditColumns,
    Base,
    ObservationColumns,
    SourceColumns,
    nonblank,
    processing_checks,
)


def valid_polygon():
    return CheckConstraint(
        "NOT ST_IsEmpty(geometry) AND ST_IsValid(geometry)", name="valid_polygon"
    )


def population_checks():
    return (
        CheckConstraint("min_people >= 0 AND max_people >= 0", name="people_nonnegative"),
        CheckConstraint("min_people <= max_people", name="people_order"),
        CheckConstraint(
            "congestion IN ('relaxed','normal','slightly_crowded','crowded')", name="congestion"
        ),
    )


class PopulationArea(AuditColumns, SourceColumns, Base):
    __tablename__ = "population_areas"
    __table_args__ = (
        *nonblank("area_code", "name"),
        *processing_checks(),
        valid_polygon(),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude"),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="coordinate_pair"),
    )

    area_code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    district: Mapped[str | None] = mapped_column(Text, index=True)
    geometry = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)


class PopulationLatest(ObservationColumns, Base):
    __tablename__ = "population_latest"
    __table_args__ = (*nonblank("processor_version"), *population_checks())

    source: Mapped[str] = mapped_column(
        Text, ForeignKey("data_sources.source_code", ondelete="RESTRICT"), primary_key=True
    )
    area_code: Mapped[str] = mapped_column(
        Text, ForeignKey("population_areas.area_code", ondelete="RESTRICT"), primary_key=True
    )
    min_people: Mapped[int | None] = mapped_column(Integer)
    max_people: Mapped[int | None] = mapped_column(Integer)
    congestion: Mapped[str | None] = mapped_column(Text)
    is_replacement: Mapped[bool | None] = mapped_column(Boolean)


class AdministrativeArea(AuditColumns, SourceColumns, Base):
    __tablename__ = "administrative_areas"
    __table_args__ = (
        *processing_checks(),
        *nonblank(
            "dong_code",
            "name",
            "sigungu_code",
            "code_system",
            "boundary_version",
            "road_mapping_version",
        ),
        valid_polygon(),
    )

    dong_code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    sigungu_code: Mapped[str] = mapped_column(Text, index=True)
    code_system: Mapped[str] = mapped_column(Text)
    geometry = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)
    boundary_version: Mapped[str] = mapped_column(Text)
    road_mapping_version: Mapped[str | None] = mapped_column(Text)


class RegionBoundary(AuditColumns, SourceColumns, Base):
    __tablename__ = "region_boundaries"
    __table_args__ = (
        *processing_checks(),
        *nonblank("code_system", "region_code", "name", "boundary_version"),
        valid_polygon(),
    )

    code_system: Mapped[str] = mapped_column(Text, primary_key=True)
    region_code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    geometry = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)
    boundary_version: Mapped[str] = mapped_column(Text)
