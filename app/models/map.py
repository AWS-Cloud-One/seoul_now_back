"""Published map contract, independent of collector storage formats."""

from datetime import datetime
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MapLayerVersion(Base):
    __tablename__ = "map_layer_versions"

    layer: Mapped[str] = mapped_column(String(20), primary_key=True)
    geometry_version: Mapped[str] = mapped_column(String(100))
    is_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    coverage: Mapped[str] = mapped_column(String(500))


class MapFeature(Base):
    __tablename__ = "map_features"

    layer: Mapped[str] = mapped_column(ForeignKey("map_layer_versions.layer"), primary_key=True)
    feature_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    geometry = mapped_column(Geometry("GEOMETRY", srid=4326), nullable=False)
    properties: Mapped[dict] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MapObservation(Base):
    __tablename__ = "map_observations"

    layer: Mapped[str] = mapped_column(String(20), primary_key=True)
    feature_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collection_failed: Mapped[bool] = mapped_column(Boolean, default=False)


class ParkingFacility(Base):
    __tablename__ = "parking_facilities"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    identity_status: Mapped[str] = mapped_column(String(20), default="unverified")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ParkingSourceRecord(Base):
    __tablename__ = "parking_source_records"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    parking_id: Mapped[UUID] = mapped_column(ForeignKey("parking_facilities.id"), index=True)
    legacy_record_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(80))
    reference_date: Mapped[str | None] = mapped_column(String(10))
    registered: Mapped[dict] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
