"""Service-link geometry, official ID correspondence and typed traffic measurements."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import (
    AuditColumns,
    Base,
    ObservationColumns,
    SourceColumns,
    nonblank,
    nonnegative,
    processing_checks,
)


class RoadLink(AuditColumns, SourceColumns, Base):
    __tablename__ = "road_links"
    __table_args__ = (
        *processing_checks(),
        *nonblank("link_id"),
        CheckConstraint(
            "NOT ST_IsEmpty(geometry) AND ST_IsValid(geometry) AND ST_Length(geometry) > 0",
            name="valid_line",
        ),
    )

    link_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str | None] = mapped_column(Text)
    geometry = mapped_column(Geometry("MULTILINESTRING", srid=4326), nullable=False)


class RoadLinkMapping(AuditColumns, Base):
    __tablename__ = "road_link_mappings"
    __table_args__ = (
        *processing_checks(),
        *nonblank("standard_link_id", "source_version"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
    )

    service_link_id: Mapped[str] = mapped_column(
        Text, ForeignKey("road_links.link_id", ondelete="RESTRICT"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    standard_link_id: Mapped[str] = mapped_column(Text, primary_key=True, index=True)
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("data_sources.source_code", ondelete="RESTRICT")
    )
    source_version: Mapped[str] = mapped_column(Text)
    reference_date: Mapped[date | None] = mapped_column(Date)
    last_payload_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("raw_payloads.payload_id", ondelete="RESTRICT"), index=True
    )
    processor_version: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoadTrafficLatest(ObservationColumns, Base):
    __tablename__ = "road_traffic_latest"
    __table_args__ = (
        *nonblank("processor_version"),
        *nonnegative("speed_kmh", "travel_seconds"),
    )

    source: Mapped[str] = mapped_column(
        Text, ForeignKey("data_sources.source_code", ondelete="RESTRICT"), primary_key=True
    )
    link_id: Mapped[str] = mapped_column(
        Text, ForeignKey("road_links.link_id", ondelete="RESTRICT"), primary_key=True
    )
    speed_kmh: Mapped[Decimal | None] = mapped_column(Numeric)
    travel_seconds: Mapped[Decimal | None] = mapped_column(Numeric)


class RoadAreaMapping(Base):
    __tablename__ = "road_area_mappings"
    __table_args__ = (*nonblank("boundary_version", "road_geometry_version", "processor_version"),)

    link_id: Mapped[str] = mapped_column(
        Text, ForeignKey("road_links.link_id", ondelete="RESTRICT"), primary_key=True
    )
    dong_code: Mapped[str] = mapped_column(
        Text,
        ForeignKey("administrative_areas.dong_code", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    boundary_version: Mapped[str] = mapped_column(Text)
    road_geometry_version: Mapped[str] = mapped_column(Text)
    processor_version: Mapped[str] = mapped_column(Text)
    mapped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
