"""Place reuse schema; provider/cache writers can be connected independently."""

from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, nonblank


class PlaceCategory(Base):
    __tablename__ = "place_categories"
    __table_args__ = (*nonblank("category_code", "name"),)

    category_code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Place(Base):
    __tablename__ = "places"
    __table_args__ = (
        *nonblank("name"),
        CheckConstraint("place_id ~ '^kakao:[^[:space:]]+$'", name="provider_identity"),
        CheckConstraint("expires_at > collected_at", name="expiry_order"),
        CheckConstraint("NOT ST_IsEmpty(location::geometry)", name="nonempty_location"),
    )

    place_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    category_code: Mapped[str | None] = mapped_column(
        Text, ForeignKey("place_categories.category_code", ondelete="RESTRICT"), index=True
    )
    category_name: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Geography("POINT", srid=4326), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    road_address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    provider_url: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
