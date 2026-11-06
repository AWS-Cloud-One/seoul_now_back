"""Shared API_SPEC response envelope, errors and source information."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ErrorDetail(ContractModel):
    field: str
    code: str
    message: str


class ApiError(ContractModel):
    code: str
    message: str
    retryable: bool
    retry_after_seconds: int | None = Field(default=None, ge=0)
    details: list[ErrorDetail] = Field(default_factory=list)


class ApiEnvelope[T](ContractModel):
    request_id: UUID
    server_time: AwareDatetime
    data: T | None
    error: ApiError | None


class ErrorResponse(ApiEnvelope[None]):
    data: None = None
    error: ApiError


class Information[T](ContractModel):
    value: T | None
    status: Literal["available", "unavailable", "unsupported", "error"]
    freshness: Literal["fresh", "stale", "unknown", "not_applicable"]
    observed_at: AwareDatetime | None
    collected_at: AwareDatetime | None
    source: str | None
    reason_code: str | None
