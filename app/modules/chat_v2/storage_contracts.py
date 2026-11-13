"""Internal database boundaries, not public HTTP schemas or model arguments."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr

from app.modules.chat_v2.contracts import (
    ConversationContext,
    PendingHeadcountQuestion,
    RunIdentity,
    Version,
)
from app.modules.chat_v2.dialogue_context import (
    DestinationArea,
    PendingRead,
    PlaceReference,
    RecommendationDraft,
    RecommendationFlow,
)
from app.modules.chat_v2.presentation_contracts import QuestionView, UserMessage
from app.modules.chat_v2.responses import ChatResponse
from app.schemas.common import ContractModel
from app.schemas.recommendations import Conditions


class SessionCredential(ContractModel):
    session_id: UUID
    token: SecretStr
    expires_at: AwareDatetime


class StoredContext(ContractModel):
    schema_version: Literal[1] = 1
    pending_question: PendingHeadcountQuestion | None = None
    excluded: list[str] = Field(default_factory=list)
    search_area: DestinationArea | None = None
    draft: RecommendationDraft | None = None
    references: list[PlaceReference] = Field(default_factory=list, max_length=30)
    focus_ref: str | None = None
    pending_read: PendingRead | None = None
    active_flow: RecommendationFlow | None = None


class ConversationSnapshot(ContractModel):
    conversation_id: UUID
    trip_id: UUID
    condition_version: Version
    state_version: Version
    conditions: Conditions
    pending_question: PendingHeadcountQuestion | None
    active_run_id: UUID | None
    expires_at: AwareDatetime
    questions: list[QuestionView] = Field(default_factory=list, max_length=2)

    search_area: DestinationArea | None = None
    draft: RecommendationDraft | None = None
    references: list[PlaceReference] = Field(default_factory=list, max_length=30)
    focus_ref: str | None = None
    pending_read: PendingRead | None = None
    active_flow: RecommendationFlow | None = None


class ExecutionView(ContractModel):
    identity: RunIdentity
    state_version: Version
    status: Literal["accepted", "running", "reconciling", "finished"]
    response: ChatResponse | None = None
    stage: str = "running"
    event_seq: int = 0
    cancel_requested_at: AwareDatetime | None = None
    retryable_tasks: list[str] = Field(default_factory=list)
    question: QuestionView | None = None


class HistoryTurn(ContractModel):
    sequence: int
    created_at: AwareDatetime
    user_message: UserMessage
    assistant_message_id: str
    run: ExecutionView


class HistoryPage(ContractModel):
    conversation_id: UUID
    state_version: int
    snapshot_sequence: int
    items: list[HistoryTurn]
    next_cursor: str | None
    active_run_id: UUID | None


class RunEvent(ContractModel):
    event_seq: int
    type: str
    state_version: Version
    payload: dict
    created_at: AwareDatetime


@dataclass(frozen=True)
class ExecutionClaim:
    context: ConversationContext
    owner: UUID = field(repr=False)
    excluded: tuple[str, ...]
    deadline_at: datetime
    approved_action: dict | None = field(default=None, repr=False)


class StorageRejected(Exception):
    """Safe admission/access error; the future HTTP adapter maps these codes."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
