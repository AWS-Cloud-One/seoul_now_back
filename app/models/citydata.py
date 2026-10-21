"""Latest complete snapshots returned by Seoul OA-21285, one per hotspot."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CityDataSnapshot(Base):
    __tablename__ = "citydata_snapshots"
    __table_args__ = (
        Index("ix_citydata_snapshots_area_name", "area_name"),
        Index("ix_citydata_snapshots_collected_at", "collected_at"),
    )

    area_code: Mapped[str] = mapped_column(String(80), primary_key=True)
    area_name: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
