"""Seoul hotspot master and population estimates by source observation time."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PopulationArea(Base):
    __tablename__ = "population_areas"
    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_population_area_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_population_area_longitude"),
        CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)", name="ck_population_area_coords"
        ),
    )

    area_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    area_name: Mapped[str] = mapped_column(String(160), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    district: Mapped[str | None] = mapped_column(String(100), index=True)


class FloatingPopulationHistory(Base):
    __tablename__ = "floating_population_history"
    __table_args__ = (
        CheckConstraint("population_min >= 0", name="ck_population_min_nonnegative"),
        CheckConstraint("population_max >= population_min", name="ck_population_range"),
        CheckConstraint("male_rate BETWEEN 0 AND 100", name="ck_population_male_rate"),
        CheckConstraint("female_rate BETWEEN 0 AND 100", name="ck_population_female_rate"),
        CheckConstraint("age_20s_rate BETWEEN 0 AND 100", name="ck_population_age20_rate"),
        CheckConstraint("age_30s_rate BETWEEN 0 AND 100", name="ck_population_age30_rate"),
    )

    area_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("population_areas.area_code"), primary_key=True
    )
    base_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    population_min: Mapped[int] = mapped_column(Integer, nullable=False)
    population_max: Mapped[int] = mapped_column(Integer, nullable=False)
    congestion_level: Mapped[str | None] = mapped_column(String(40))
    male_rate: Mapped[float | None] = mapped_column(Float)
    female_rate: Mapped[float | None] = mapped_column(Float)
    age_20s_rate: Mapped[float | None] = mapped_column(Float)
    age_30s_rate: Mapped[float | None] = mapped_column(Float)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
