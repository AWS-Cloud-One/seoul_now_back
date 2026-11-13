"""Public question/history metadata; action arguments remain server-owned."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.schemas.common import ContractModel


class ChoiceDefinition(ContractModel):
    key: str
    label: str
    description: str | None = None
    target_ref: str | None = None
    collection_id: UUID | None = None
    decision: dict


class QuestionDefinition(ContractModel):
    clarification_id: str
    prompt: str
    required_field: Literal[
        "headcount", "origin", "purposes", "search_area", "lookup_origin", "target"
    ]
    scope: Literal["persistent", "request_only", "lookup"]
    expected_answer_type: Literal["integer", "text", "choice"]
    choices: list[ChoiceDefinition] = Field(default_factory=list, max_length=6)


class ActionView(ContractModel):
    action_id: UUID
    type: Literal["choose"] = "choose"
    label: str
    description: str | None = None
    target_ref: str | None = None
    collection_id: UUID | None = None
    expires_at: AwareDatetime
    expected_condition_version: int
    expected_state_version: int
    status: Literal["available", "expired", "stale", "busy", "consumed"]
    consumed_by_run_id: UUID | None = None


class QuestionView(ContractModel):
    clarification_id: str
    source_run_id: UUID
    prompt: str
    required_field: str
    scope: Literal["persistent", "request_only", "lookup"]
    expected_answer_type: Literal["integer", "text", "choice"]
    allow_free_text: Literal[True] = True
    status: Literal["active", "resolved"]
    options: list[ActionView] = Field(default_factory=list, max_length=6)


class UserMessage(ContractModel):
    message_id: str
    input_type: Literal["message", "action", "retry"]
    text: str
    source_run_id: UUID | None = None
