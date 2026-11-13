"""V2 ownership and atomic F02 receipts; V1 trips are never adopted."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ChatUsageBucket(Base):
    __tablename__ = "chat_usage_buckets"
    __table_args__ = (CheckConstraint("used >= 0", name="usage"),)
    bucket: Mapped[str] = mapped_column(String(160), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    used: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ChatBrowserSession(Base):
    __tablename__ = "chat_browser_sessions"
    __table_args__ = (
        CheckConstraint("token_digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("expires_at > created_at", name="expiry"),
    )
    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ChatConversation(Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        CheckConstraint("state_version >= 0", name="version"),
        CheckConstraint("expires_at > created_at", name="expiry"),
        CheckConstraint("context->>'schema_version' = '1'", name="context_schema"),
        UniqueConstraint("conversation_id", "trip_id"),
        ForeignKeyConstraint(
            ["conversation_id", "active_run_id"],
            ["chat_runs.conversation_id", "chat_runs.run_id"],
            name="fk_chat_conversations_active_run",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    conversation_id: Mapped[UUID] = mapped_column(primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_browser_sessions.session_id"), index=True
    )
    trip_id: Mapped[UUID] = mapped_column(ForeignKey("chat_sessions.trip_id"), unique=True)
    state_version: Mapped[int] = mapped_column(Integer)
    context: Mapped[dict] = mapped_column(JSONB)
    active_run_id: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ChatRun(Base):
    __tablename__ = "chat_runs"
    __table_args__ = (
        UniqueConstraint("conversation_id", "request_id", name="uq_chat_runs_request"),
        UniqueConstraint("conversation_id", "sequence", name="uq_chat_runs_sequence"),
        CheckConstraint("sequence > 0", name="sequence"),
        UniqueConstraint("conversation_id", "run_id", name="uq_chat_runs_identity"),
        UniqueConstraint("run_id", "trip_id"),
        ForeignKeyConstraint(
            ["conversation_id", "trip_id"],
            ["chat_conversations.conversation_id", "chat_conversations.trip_id"],
        ),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash"),
        CheckConstraint(
            "phase IN ('accepted','running','preparing_commit','post_commit','finished')",
            name="phase",
        ),
        CheckConstraint(
            "(phase IN ('finished','post_commit')) = (response IS NOT NULL)", name="response"
        ),
        CheckConstraint("deadline_at > created_at", name="deadline"),
        CheckConstraint("event_seq >= 0 AND event_seq <= 200", name="event_seq"),
    )
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column()
    trip_id: Mapped[UUID] = mapped_column()
    request_id: Mapped[UUID] = mapped_column()
    sequence: Mapped[int] = mapped_column(Integer)
    question: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    user_message: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    request_aliases: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    request_hash: Mapped[str] = mapped_column(String(64))
    owner: Mapped[UUID] = mapped_column()
    request: Mapped[dict] = mapped_column(JSONB)
    context: Mapped[dict] = mapped_column(JSONB)
    prepared: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    response: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    phase: Mapped[str] = mapped_column(Text)
    completed_state_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    implementation_version: Mapped[str] = mapped_column(Text, server_default="p5-1")
    stage: Mapped[str] = mapped_column(Text, server_default="accepted")
    event_seq: Mapped[int] = mapped_column(Integer, server_default="0")
    operation: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


class ChatAction(Base):
    __tablename__ = "chat_actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "source_run_id"],
            ["chat_runs.conversation_id", "chat_runs.run_id"],
            name="fk_chat_actions_source",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "consumed_by_run_id"],
            ["chat_runs.conversation_id", "chat_runs.run_id"],
            name="fk_chat_actions_consumed",
        ),
        CheckConstraint("expires_at > created_at", name="expiry"),
        CheckConstraint("condition_version >= 0 AND state_version >= 0", name="versions"),
    )
    action_id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(index=True)
    source_run_id: Mapped[UUID] = mapped_column(index=True)
    clarification_id: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[dict] = mapped_column(JSONB)
    condition_version: Mapped[int] = mapped_column(Integer)
    state_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_by_run_id: Mapped[UUID | None] = mapped_column()


class ChatRunEvent(Base):
    __tablename__ = "chat_run_events"
    __table_args__ = (CheckConstraint("event_seq > 0 AND event_seq <= 200", name="sequence"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("chat_runs.run_id"), primary_key=True)
    event_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    state_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChatResult(Base):
    """V2 result identity; immutable facts live in the run's atomic final response."""

    __tablename__ = "chat_results"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "trip_id"], ["chat_runs.run_id", "chat_runs.trip_id"]),
        UniqueConstraint("trip_id", "result_id"),
    )
    result_id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(unique=True)
    trip_id: Mapped[UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChatRunCommit(Base):
    __tablename__ = "chat_run_commits"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "trip_id"], ["chat_runs.run_id", "chat_runs.trip_id"]),
        ForeignKeyConstraint(
            ["trip_id", "result_id"],
            ["chat_results.trip_id", "chat_results.result_id"],
        ),
        ForeignKeyConstraint(
            ["trip_id", "current_version"],
            ["chat_condition_versions.trip_id", "chat_condition_versions.version"],
        ),
        CheckConstraint(
            "previous_version >= 0 AND current_version "
            "BETWEEN previous_version AND previous_version + 1",
            name="version",
        ),
    )
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    trip_id: Mapped[UUID] = mapped_column()
    previous_version: Mapped[int] = mapped_column(Integer)
    current_version: Mapped[int] = mapped_column(Integer)
    result_id: Mapped[UUID | None] = mapped_column()
    receipt: Mapped[dict] = mapped_column(JSONB)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
