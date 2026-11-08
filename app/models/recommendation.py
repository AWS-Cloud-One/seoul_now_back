"""Historical result snapshots in revision 0013; API writers are not connected yet.

Conditions reference an immutable chat condition version. Card facts are copied,
never read back from the mutable places/parking caches as historical evidence.
These tables do not own recommendation search progress or request idempotency.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from geoalchemy2 import Geography
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, nonblank, nonnegative


class RecommendationResult(Base):
    __tablename__ = "recommendation_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trip_id", "condition_version"],
            ["chat_condition_versions.trip_id", "chat_condition_versions.version"],
            ondelete="RESTRICT",
        ),
        # Retry source must belong to this trip. The service compares actual condition
        # values: reverting an edit can yield equal conditions at a newer version.
        UniqueConstraint("trip_id", "result_id"),
        ForeignKeyConstraint(
            ["trip_id", "source_result_id"],
            [
                "recommendation_results.trip_id",
                "recommendation_results.result_id",
            ],
            name="fk_recommendation_results_retry_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("result_id", "kind"),
        CheckConstraint("condition_version >= 0", name="condition_version"),
        CheckConstraint("source_result_id <> result_id", name="retry_not_self"),
        CheckConstraint("kind IN ('recommendation','general_candidates')", name="result_kind"),
        CheckConstraint(
            "(kind = 'recommendation' AND routes_status IN ('ok','partial') "
            "AND explanation_status IN ('ok','fallback')) OR "
            "(kind = 'general_candidates' AND routes_status = 'failed' "
            "AND explanation_status = 'skipped')",
            name="result_stages",
        ),
        CheckConstraint("context_expires_at > created_at", name="context_expiry"),
        CheckConstraint("context_token_digest ~ '^[0-9a-f]{64}$'", name="context_digest"),
        Index("ix_recommendation_results_trip_created", "trip_id", "created_at"),
        # Correlation only: in-flight/completed request replay is owned by part 2.
        Index("ix_recommendation_results_trip_request", "trip_id", "request_id"),
    )

    result_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    trip_id: Mapped[UUID] = mapped_column()
    condition_version: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(Text)
    source_result_id: Mapped[UUID | None] = mapped_column()
    routes_status: Mapped[str] = mapped_column(Text)
    explanation_status: Mapped[str] = mapped_column(Text)
    # The original result time must be supplied; insertion time is not a substitute.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    context_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # SHA-256 of an opaque high-entropy token. Never store the bearer value.
    context_token_digest: Mapped[str] = mapped_column(String(64), unique=True)


class RecommendationCard(Base):
    __tablename__ = "recommendation_cards"
    __table_args__ = (
        ForeignKeyConstraint(
            ["result_id", "result_kind"],
            ["recommendation_results.result_id", "recommendation_results.kind"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("result_id", "position", name="uq_recommendation_cards_position"),
        # Evidence can only attach to a successful recommendation card.
        UniqueConstraint(
            "result_id", "place_id", "result_kind", name="uq_recommendation_cards_kind"
        ),
        CheckConstraint("position BETWEEN 0 AND 2", name="card_position"),
        CheckConstraint("place_id ~ '^kakao:[^[:space:]]+$'", name="place_identity"),
        *nonblank("place_name", "reason_text"),
        CheckConstraint("NOT ST_IsEmpty(place_location::geometry)", name="place_location"),
        CheckConstraint("purpose IN ('cafe','food','walk','exhibition')", name="place_purpose"),
        CheckConstraint(
            "place_freshness IN ('fresh','stale','unknown','not_applicable')",
            name="place_freshness",
        ),
        CheckConstraint(
            "route_status IN ('available','unavailable','unsupported','error')",
            name="route_status",
        ),
        CheckConstraint(
            "route_freshness IN ('fresh','stale','unknown','not_applicable')",
            name="route_freshness",
        ),
        CheckConstraint(
            "(result_kind = 'recommendation' AND rank IS NOT NULL AND rank = position + 1 "
            "AND route_status = 'available' AND duration_seconds IS NOT NULL "
            "AND distance_meters IS NOT NULL AND reason_source IN ('ai','fallback')) OR "
            "(result_kind = 'general_candidates' AND rank IS NULL "
            "AND route_status IN ('unavailable','error') AND duration_seconds IS NULL "
            "AND distance_meters IS NULL AND reason_source = 'none')",
            name="card_kind_facts",
        ),
        CheckConstraint("duration_seconds >= 0", name="duration_nonnegative"),
        *nonnegative("distance_meters", "parking_nearest_distance_meters"),
        CheckConstraint(
            "parking_status IN ('available','unavailable','error')", name="parking_status"
        ),
        CheckConstraint("parking_search_radius_meters IN (300,500,1000)", name="parking_radius"),
        CheckConstraint("parking_total_count >= 0", name="parking_count"),
        CheckConstraint(
            "parking_status <> 'available' OR "
            "(parking_search_radius_meters IS NOT NULL AND parking_total_count IS NOT NULL "
            "AND parking_total_count > 0)",
            name="available_parking",
        ),
    )

    result_id: Mapped[UUID] = mapped_column(primary_key=True)
    place_id: Mapped[str] = mapped_column(Text, primary_key=True)
    result_kind: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer)
    # No FK to mutable places/categories: historical identity and facts survive refreshes.
    place_name: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    category_code: Mapped[str | None] = mapped_column(Text)
    category_name: Mapped[str | None] = mapped_column(Text)
    place_location = mapped_column(
        Geography("POINT", srid=4326, spatial_index=False), nullable=False
    )
    address: Mapped[str | None] = mapped_column(Text)
    road_address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    provider_url: Mapped[str | None] = mapped_column(Text)
    # Place status is always available: an identified candidate is required for a card.
    place_freshness: Mapped[str] = mapped_column(Text)
    place_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    place_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    place_source: Mapped[str | None] = mapped_column(Text)
    place_reason_code: Mapped[str | None] = mapped_column(Text)

    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    distance_meters: Mapped[Decimal | None] = mapped_column(Numeric)
    route_status: Mapped[str] = mapped_column(Text)
    route_freshness: Mapped[str] = mapped_column(Text)
    route_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    route_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    route_source: Mapped[str | None] = mapped_column(Text)
    route_reason_code: Mapped[str | None] = mapped_column(Text)

    # Retain the summary used at recommendation time, not the whole provider response.
    parking_status: Mapped[str] = mapped_column(Text)
    parking_reason_code: Mapped[str | None] = mapped_column(Text)
    parking_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parking_search_radius_meters: Mapped[int | None] = mapped_column(Integer)
    parking_nearest_distance_meters: Mapped[Decimal | None] = mapped_column(Numeric)
    parking_total_count: Mapped[int | None] = mapped_column(Integer)
    reason_text: Mapped[str] = mapped_column(Text)
    reason_source: Mapped[str] = mapped_column(Text)


class RecommendationEvidence(Base):
    __tablename__ = "recommendation_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["result_id", "place_id", "result_kind"],
            [
                "recommendation_cards.result_id",
                "recommendation_cards.place_id",
                "recommendation_cards.result_kind",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint("result_kind = 'recommendation'", name="recommended_evidence"),
        CheckConstraint("position >= 0", name="evidence_position"),
        *nonblank("text"),
        # These are the two evidence value shapes currently emitted by cards.py.
        # A new fact kind needs an explicit relational schema, not an arbitrary JSON blob.
        CheckConstraint(
            "(type = 'eta' AND duration_seconds IS NOT NULL AND distance_meters IS NOT NULL) "
            "OR (type = 'parking' AND duration_seconds IS NULL AND distance_meters IS NOT NULL)",
            name="evidence_value",
        ),
        CheckConstraint("duration_seconds >= 0", name="duration_nonnegative"),
        *nonnegative("distance_meters"),
        CheckConstraint(
            "freshness IN ('fresh','stale','unknown','not_applicable')", name="freshness"
        ),
    )

    result_id: Mapped[UUID] = mapped_column(primary_key=True)
    place_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    result_kind: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    # Only verified, available values can be recommendation reasons.
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    distance_meters: Mapped[Decimal | None] = mapped_column(Numeric)
    freshness: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
