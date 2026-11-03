"""Relational session/message foundations, without implementing chat or recommendation APIs."""

from datetime import datetime
from uuid import UUID, uuid4

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import AuditColumns, Base, nonblank


class ChatSession(AuditColumns, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        CheckConstraint("condition_version >= 0", name="condition_version"),
        CheckConstraint("updated_at >= created_at", name="update_order"),
        CheckConstraint("expires_at > created_at", name="expiry_order"),
        # A session and its initial condition version are inserted in one transaction.
        # Defer the current-version pointer to commit, breaking the insertion cycle.
        ForeignKeyConstraint(
            ["trip_id", "condition_version"],
            ["chat_condition_versions.trip_id", "chat_condition_versions.version"],
            name="fk_chat_sessions_current_condition",
            # Default NO ACTION is deferred too: child rows and an expired session
            # can be explicitly removed together without an undeletable FK cycle.
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    trip_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    condition_version: Mapped[int] = mapped_column(Integer, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatConditionVersion(Base):
    __tablename__ = "chat_condition_versions"
    __table_args__ = (
        CheckConstraint("version >= 0", name="version"),
        *nonblank("origin_label"),
        CheckConstraint(
            "(num_nonnulls(origin_label,origin_location,origin_supported) = 0 "
            "AND origin_address IS NULL AND origin_region IS NULL) OR "
            "num_nonnulls(origin_label,origin_location,origin_supported) = 3",
            name="origin_fields",
        ),
        CheckConstraint(
            "origin_region IN ('seoul','incheon','gyeonggi','other')", name="origin_region"
        ),
        CheckConstraint("NOT ST_IsEmpty(origin_location::geometry)", name="origin_nonempty"),
        CheckConstraint(
            "group_type IN ('solo','date','friends','family','other')", name="group_type"
        ),
        CheckConstraint("headcount > 0", name="headcount"),
        CheckConstraint("max_travel_seconds > 0", name="max_travel_seconds"),
    )

    trip_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.trip_id", ondelete="RESTRICT"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    origin_label: Mapped[str | None] = mapped_column(Text)
    origin_location = mapped_column(Geography("POINT", srid=4326), nullable=True)
    origin_address: Mapped[str | None] = mapped_column(Text)
    origin_region: Mapped[str | None] = mapped_column(Text)
    origin_supported: Mapped[bool | None] = mapped_column(Boolean)
    group_type: Mapped[str | None] = mapped_column(Text)
    headcount: Mapped[int | None] = mapped_column(Integer)
    max_travel_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def condition_reference():
    return ForeignKeyConstraint(
        ["trip_id", "condition_version"],
        ["chat_condition_versions.trip_id", "chat_condition_versions.version"],
        ondelete="RESTRICT",
    )


class ChatConditionPurpose(Base):
    __tablename__ = "chat_condition_purposes"
    __table_args__ = (
        condition_reference(),
        UniqueConstraint("trip_id", "condition_version", "position"),
        CheckConstraint("purpose IN ('cafe','food','walk','exhibition')", name="purpose"),
        # Initial conditions may have no purposes; accepted conditions have at most two.
        CheckConstraint("position BETWEEN 0 AND 1", name="position"),
    )

    trip_id: Mapped[UUID] = mapped_column(primary_key=True)
    condition_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    keyword: Mapped[str | None] = mapped_column(String(100))


class ChatConditionAgeGroup(Base):
    __tablename__ = "chat_condition_age_groups"
    __table_args__ = (
        condition_reference(),
        UniqueConstraint("trip_id", "condition_version", "position"),
        CheckConstraint(
            "age_group IN ('under_10','teens','twenties','thirties',"
            "'forties','fifties','sixty_plus')",
            name="age_group",
        ),
        CheckConstraint("position BETWEEN 0 AND 6", name="position"),
    )

    trip_id: Mapped[UUID] = mapped_column(primary_key=True)
    condition_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    age_group: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class ChatExcludedPlace(Base):
    __tablename__ = "chat_excluded_places"

    trip_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.trip_id", ondelete="RESTRICT"), primary_key=True
    )
    place_id: Mapped[str] = mapped_column(
        Text, ForeignKey("places.place_id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    excluded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("trip_id", "sequence"),
        condition_reference(),
        ForeignKeyConstraint(
            ["trip_id", "base_condition_version"],
            ["chat_condition_versions.trip_id", "chat_condition_versions.version"],
            name="fk_chat_messages_base_condition",
            ondelete="RESTRICT",
        ),
        CheckConstraint("sequence >= 1", name="sequence"),
        CheckConstraint("role IN ('user','assistant')", name="role"),
        CheckConstraint(
            "kind IN ('clarification','recommendation','general_candidates',"
            "'no_candidates','failure','answer')",
            name="kind",
        ),
        CheckConstraint("kind IS NULL OR role = 'assistant'", name="assistant_kind"),
        CheckConstraint(
            "condition_applied IS NULL OR "
            "(base_condition_version IS NOT NULL AND condition_version IS NOT NULL)",
            name="applied_versions",
        ),
        CheckConstraint("condition_version >= base_condition_version", name="version_order"),
        CheckConstraint(
            "condition_applied IS DISTINCT FROM FALSE OR "
            "condition_version = base_condition_version",
            name="unapplied_version",
        ),
        CheckConstraint(
            "interpretation_status IN ('skipped','ok','failed')", name="interpretation_status"
        ),
        CheckConstraint(
            "routes_status IN ('skipped','ok','partial','failed')", name="routes_status"
        ),
        CheckConstraint(
            "explanation_status IN ('skipped','ok','fallback')", name="explanation_status"
        ),
    )

    message_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    trip_id: Mapped[UUID] = mapped_column(ForeignKey("chat_sessions.trip_id", ondelete="RESTRICT"))
    sequence: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[UUID | None] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(Text)
    base_condition_version: Mapped[int | None] = mapped_column(Integer)
    condition_version: Mapped[int | None] = mapped_column(Integer)
    condition_applied: Mapped[bool | None] = mapped_column(Boolean)
    interpretation_status: Mapped[str | None] = mapped_column(Text)
    routes_status: Mapped[str | None] = mapped_column(Text)
    explanation_status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
