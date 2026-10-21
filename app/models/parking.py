"""Source records, not deduplicated physical facilities or live vacancies."""

from datetime import datetime
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24))
    received: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    area_code: Mapped[str | None] = mapped_column(String(80), index=True)
    area_name: Mapped[str | None] = mapped_column(String(160))
    rejection_reasons: Mapped[dict] = mapped_column(JSONB, default=dict)


class ParkingRecord(Base):
    __tablename__ = "parking_records"
    __table_args__ = (Index("ix_parking_records_location", "location", postgresql_using="gist"),)

    # Fingerprint includes source, original code, name, address and coordinates.
    # A source code alone is NOT a unique facility identifier.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    source_id: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
