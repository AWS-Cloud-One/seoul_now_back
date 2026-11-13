"""Public HTTP envelopes, shared by OpenAPI and the frontend handoff bundle."""

from uuid import UUID

from pydantic import AwareDatetime

from app.modules.chat_v2.storage_contracts import ExecutionView, RunEvent
from app.schemas.common import ContractModel


class SessionCreated(ContractModel):
    session_id: UUID
    session_token: str
    expires_at: AwareDatetime


class SubmitResult(ContractModel):
    run: ExecutionView
    status_url: str


class RunPoll(ContractModel):
    run: ExecutionView
    events: list[RunEvent]


class CancelRequest(ContractModel):
    request_id: UUID


class ErrorCode(ContractModel):
    code: str


class ErrorEnvelope(ContractModel):
    error: ErrorCode
